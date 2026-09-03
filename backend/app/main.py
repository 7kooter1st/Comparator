import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
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
    ResultRequest,
    ResultResponse,
)
from app.services.job_ingress import DurableJobIngress
from app.services.object_store import get_object_store
from app.services.outbox import OutboxPublisher
from app.services.kafka_producer import (
    get_kafka_producer,
    is_kafka_producer_ready,
    start_kafka_producer,
    stop_kafka_producer,
)
from app.services.ollama import OllamaError, parse_comparison_result
from app.services.processing_client import (
    ProcessingServiceUnavailable,
    get_health,
)
from app.services.ws_relay import relay_job_to_client
from app.workers.prepare_worker import PrepareWorker
from app.workflow.repository import WorkflowRepository

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
    "live",
    "ready",
    "metrics",
    "ws/",
    "static/",
)

_outbox: OutboxPublisher | None = None
_prepare_worker: PrepareWorker | None = None
_ready = False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _outbox, _prepare_worker, _ready
    await database.start()
    await database.bootstrap_admin()
    try:
        await start_kafka_producer()
    except Exception:
        logger.exception("[Kafka] producer unavailable at startup; outbox will retry")
    producer = get_kafka_producer()
    workflow = WorkflowRepository(database.pool)
    if producer is not None:
        _outbox = OutboxPublisher(workflow, producer)
        await _outbox.start()
    _prepare_worker = PrepareWorker(workflow, database.pool)
    await _prepare_worker.start()
    _ready = True
    yield
    _ready = False
    if _prepare_worker is not None:
        await _prepare_worker.stop()
    if _outbox is not None:
        await _outbox.stop()
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


async def _store_upload(upload: UploadFile, field_name: str, job_prefix: str, side: int):
    if not upload.filename:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Поле {field_name}: имя файла отсутствует"},
        )
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Поле {field_name}: нужен файл .pdf или .docx"},
        )
    staging_key = f"staging/{job_prefix}/file{side}{suffix}"
    try:
        stored = await get_object_store().put_fileobj(
            staging_key,
            upload.file,
            content_type=upload.content_type or "application/octet-stream",
            max_bytes=settings.max_upload_bytes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "Файл превышает допустимый размер",
                "hint": f"Максимум {settings.max_upload_mb} МБ",
            },
        ) from exc
    if stored.size_bytes == 0:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Поле {field_name}: файл пустой"},
        )
    final_key = f"jobs/{job_prefix}/originals/file{side}-{stored.sha256}{suffix}"
    if final_key != staging_key:
        data = await get_object_store().get_bytes(staging_key)
        stored = await get_object_store().put_bytes(
            final_key,
            data,
            content_type=stored.content_type,
        )
        await get_object_store().delete(staging_key)
    return stored, upload.filename, stored.content_type


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


@app.get("/live", tags=["Health"])
async def live() -> dict:
    return {"status": "ok"}


@app.get("/ready", tags=["Health"])
async def ready() -> dict:
    object_ok = await get_object_store().ping()
    if not (_ready and object_ok):
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ok", "object_store": object_ok, "outbox": _outbox is not None}


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

    object_ok = await get_object_store().ping()
    return {
        "status": "ok" if object_ok else "degraded",
        "kafka_producer": kafka_ok,
        "processing_service_reachable": processing_ok,
        "processing": processing_status,
        "object_store": object_ok,
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
    user: AuthUser = Depends(get_current_user),
    file1: UploadFile = File(..., description="Первый файл (.pdf или .docx)"),
    file2: UploadFile = File(..., description="Второй файл (.pdf или .docx)"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CompareResponse:
    if not await get_object_store().ping():
        return JSONResponse(
            status_code=503,
            content={
                "error": "Хранилище файлов недоступно",
                "hint": "Попробуйте ещё раз через минуту.",
            },
        )

    ingress = DurableJobIngress(database.pool)
    if idempotency_key:
        existing = await ingress.lookup_idempotency(user.id, idempotency_key)
        if existing is not None:
            existing.pop("_request_hash", None)
            return CompareResponse.model_validate(existing)

    job_id = str(uuid.uuid4())
    stored1, name1, _type1 = await _store_upload(file1, "file1", job_id, 1)
    stored2, name2, _type2 = await _store_upload(file2, "file2", job_id, 2)
    try:
        payload, _created = await ingress.create_or_resume(
            user_id=user.id,
            file1_name=name1,
            file2_name=name2,
            object1={
                "key": stored1.key,
                "sha256": stored1.sha256,
                "size_bytes": stored1.size_bytes,
                "content_type": stored1.content_type,
            },
            object2={
                "key": stored2.key,
                "sha256": stored2.sha256,
                "size_bytes": stored2.size_bytes,
                "content_type": stored2.content_type,
            },
            idempotency_key=idempotency_key,
            job_id=job_id,
        )
    except Exception:
        await get_object_store().delete_prefix(f"jobs/{job_id}")
        raise

    logger.info(
        "[Compare] job_id=%s user=%s accepted | file1=%s | file2=%s",
        payload["job_id"],
        user.username,
        name1,
        name2,
    )
    return CompareResponse.model_validate(payload)


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
