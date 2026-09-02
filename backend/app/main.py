import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.config import settings
from app.db import database
from app.deps import authenticate_websocket, get_current_user
from app.logging_config import setup_logging
from app.models import AuthUser
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.jobs import require_owned_job, router as jobs_router
from app.schemas import (
    CompareResponse,
    ComparisonResult,
    ErrorResponse,
    FileChunkStats,
    ResultRequest,
    ResultResponse,
)
from app.services.compare_jobs import mark_job_accepted, run_compare_pipeline
from app.services.file_store import save_original
from app.services.kafka_producer import (
    is_kafka_producer_ready,
    start_kafka_producer,
    stop_kafka_producer,
)
from app.services.ollama import OllamaError, parse_comparison_result
from app.services.processing_client import (
    ProcessingServiceUnavailable,
    get_health,
    register_job,
)
from app.services.ws_relay import relay_job_to_client

setup_logging()
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
DIST_DIR = ROOT_DIR / "dist"
LEGACY_FRONTEND_DIR = ROOT_DIR / "frontend"
# Production Vite build takes priority over the old single-file frontend.
FRONTEND_DIR = (
    DIST_DIR
    if (DIST_DIR / "index.html").is_file()
    else LEGACY_FRONTEND_DIR
)
_RESERVED_FRONTEND_PREFIXES = (
    "api/",
    "docs",
    "redoc",
    "openapi.json",
    "health",
    "ws/",
    "static/",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    await database.start()
    await database.bootstrap_admin()
    # A running HTTP server without Kafka accepts jobs that can never be
    # processed. Fail startup instead and let the launcher report the cause.
    await start_kafka_producer()
    yield
    await stop_kafka_producer()
    await database.stop()


app = FastAPI(
    title="Document Comparator API",
    description=(
        "Chunking & Producer + WebSocket Gateway: приём PDF/DOCX, chunking, Kafka, "
        "проксирование прогресса и результата от Processing Service на фронтенд."
    ),
    version="5.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(jobs_router)

if FRONTEND_DIR.is_dir():
    logger.info("Frontend static dir: %s", FRONTEND_DIR)
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


async def _read_upload(upload: UploadFile, field_name: str) -> tuple[bytes, str, str]:
    if not upload.filename:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Поле {field_name}: имя файла отсутствует"},
        )

    content = await upload.read()
    if not content:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Поле {field_name}: файл пустой"},
        )

    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "Файл превышает допустимый размер",
                "hint": f"Максимум {settings.max_upload_mb} МБ",
            },
        )

    content_type = upload.content_type or "application/octet-stream"
    return content, upload.filename, content_type


@app.get("/", include_in_schema=False)
async def frontend_index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)
    return JSONResponse(
        status_code=404,
        content={
            "error": "Frontend not found",
            "hint": "Положите Vite build в dist/ или создайте frontend/index.html",
        },
    )


@app.get("/health", tags=["Health"], summary="Проверка Chunking Service и Processing Service")
async def health() -> dict:
    kafka_ok = is_kafka_producer_ready()
    processing_ok = False
    processing_status: dict | str = "unavailable"

    try:
        processing_status = await get_health()
        processing_ok = processing_status.get("status") in {"ok", "degraded"}
    except ProcessingServiceUnavailable as exc:
        processing_status = str(exc)

    return {
        "status": "ok" if kafka_ok and processing_ok else "degraded",
        "kafka_producer": kafka_ok,
        "processing_service_reachable": processing_ok,
        "processing": processing_status,
    }


@app.post(
    "/api/compare",
    response_model=CompareResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    tags=["Compare"],
    summary="Разбить два файла на чанки и отправить в Kafka",
)
async def compare(
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),
    file1: UploadFile = File(..., description="Первый файл (.pdf или .docx)"),
    file2: UploadFile = File(..., description="Второй файл (.pdf или .docx)"),
) -> CompareResponse:
    if not is_kafka_producer_ready():
        return JSONResponse(
            status_code=503,
            content={
                "error": "Сервис временно недоступен",
                "hint": "Попробуйте ещё раз через минуту.",
            },
        )

    content1, name1, type1 = await _read_upload(file1, "file1")
    content2, name2, type2 = await _read_upload(file2, "file2")

    job_id = str(uuid.uuid4())
    user_id = str(user.id)
    ws_url = settings.job_websocket_url(job_id)
    stored1 = save_original(
        user_id=user_id,
        job_id=job_id,
        side=1,
        filename=name1,
        content=content1,
        content_type=type1,
    )
    stored2 = save_original(
        user_id=user_id,
        job_id=job_id,
        side=2,
        filename=name2,
        content=content2,
        content_type=type2,
    )

    try:
        await register_job(
            job_id,
            user_id=user_id,
            status="preparing",
            message="Подготовка документов…",
            file1_name=name1,
            file2_name=name2,
            required=True,
        )
        await database.insert_job_file(
            job_id=job_id,
            side=1,
            original_filename=name1,
            content_type=stored1["content_type"],
            size_bytes=stored1["size_bytes"],
            sha256=stored1["sha256"],
            storage_path=stored1["storage_path"],
        )
        await database.insert_job_file(
            job_id=job_id,
            side=2,
            original_filename=name2,
            content_type=stored2["content_type"],
            size_bytes=stored2["size_bytes"],
            sha256=stored2["sha256"],
            storage_path=stored2["storage_path"],
        )
    except ProcessingServiceUnavailable as exc:
        logger.error("[Compare ✗] job_id=%s registration failed: %s", job_id, exc)
        return JSONResponse(
            status_code=503,
            content={
                "error": "Сервис обработки не готов",
                "hint": "Задача не создана. Попробуйте ещё раз через минуту.",
            },
        )

    # Return a job_id only after Processing has confirmed registration.
    mark_job_accepted(job_id)
    background_tasks.add_task(
        run_compare_pipeline,
        job_id,
        content1,
        name1,
        content2,
        name2,
        user_id,
    )
    logger.info(
        "[Compare] job_id=%s user=%s accepted | file1=%s | file2=%s | ws=%s",
        job_id,
        user.username,
        name1,
        name2,
        ws_url,
    )

    return CompareResponse(
        job_id=job_id,
        status="preparing",
        total_chunks=0,
        kafka_topic=settings.kafka_topic_raw_chunks,
        websocket_url=ws_url,
        file1=FileChunkStats(filename=name1, format="pending", chunks=0),
        file2=FileChunkStats(filename=name2, format="pending", chunks=0),
    )


@app.websocket("/ws/jobs/{job_id}")
async def job_websocket(websocket: WebSocket, job_id: str) -> None:
    """
    WebSocket для фронтенда: проксирует события status/result/error
    от Processing Service (:5001) только владельцу задачи.
    """
    user = await authenticate_websocket(websocket, database)
    if user is None:
        await websocket.close(code=4401, reason="Требуется вход")
        return
    job = await database.get_job(job_id)
    try:
        require_owned_job(job, user)
    except HTTPException as exc:
        code = 4404 if exc.status_code == 404 else 4403
        await websocket.close(code=code, reason=str(exc.detail))
        return

    try:
        await relay_job_to_client(job_id, websocket)
    except WebSocketDisconnect:
        logger.info("job_id=%s: frontend WebSocket отключён", job_id)
    except Exception as exc:
        logger.exception("job_id=%s: ошибка WebSocket relay: %s", job_id, exc)
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011)


@app.post(
    "/api/result",
    response_model=ResultResponse,
    responses={
        400: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    tags=["Compare"],
    summary="Разобрать ответ Ollama для фронтенда",
)
async def result(
    body: ResultRequest,
    _user: AuthUser = Depends(get_current_user),
) -> ResultResponse:
    try:
        comparison = parse_comparison_result(body.ollama)
    except OllamaError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Не удалось разобрать ответ модели",
                "hint": str(exc),
            },
        ) from exc

    return ResultResponse(comparison=ComparisonResult.model_validate(comparison))


@app.exception_handler(HTTPException)
async def http_exception_handler(_request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": str(exc.detail)},
    )


@app.get("/{file_path:path}", include_in_schema=False)
async def frontend_spa(file_path: str):
    """Serve Vite dist files (favicon, icons, SPA routes). /assets is mounted above."""
    if any(
        file_path == prefix.rstrip("/") or file_path.startswith(prefix)
        for prefix in _RESERVED_FRONTEND_PREFIXES
    ):
        raise HTTPException(status_code=404, detail="Not found")

    candidate = (FRONTEND_DIR / file_path).resolve()
    try:
        candidate.relative_to(FRONTEND_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc

    if candidate.is_file():
        return FileResponse(candidate)

    index_path = FRONTEND_DIR / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)

    raise HTTPException(status_code=404, detail="Frontend not found")
