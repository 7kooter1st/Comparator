import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.config import settings
from app.schemas import (
    CompareResponse,
    ComparisonResult,
    ErrorResponse,
    FileChunkStats,
    ResultRequest,
    ResultResponse,
)
from app.services.chunk_builder import build_raw_chunk_messages
from app.services.files import PdfConversionError, prepare_file
from app.services.kafka_producer import (
    KafkaProducerError,
    publish_raw_chunks,
    start_kafka_producer,
    stop_kafka_producer,
)
from app.services.ollama import OllamaError, parse_comparison_result
from app.services.processing_client import ProcessingServiceUnavailable, get_health
from app.logging_config import setup_logging
from app.services.ws_relay import relay_job_to_client

setup_logging()
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await start_kafka_producer()
    except Exception as exc:
        logger.warning("Kafka producer не запущен при старте: %s", exc)
    yield
    await stop_kafka_producer()


app = FastAPI(
    title="Document Comparator API",
    description=(
        "Chunking & Producer + WebSocket Gateway: приём PDF/DOCX, chunking, Kafka, "
        "проксирование прогресса и результата от Processing Service на фронтенд."
    ),
    version="4.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


async def _read_upload(upload: UploadFile, field_name: str) -> tuple[bytes, str]:
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

    return content, upload.filename


@app.get("/", include_in_schema=False)
async def frontend_index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)
    return JSONResponse(
        status_code=404,
        content={"error": "Frontend not found", "hint": "Создайте frontend/index.html"},
    )


@app.get("/health", tags=["Health"], summary="Проверка Chunking Service и Processing Service")
async def health() -> dict:
    kafka_ok = True
    processing_ok = False
    processing_status: dict | str = "unavailable"

    try:
        processing_status = await get_health()
        processing_ok = processing_status.get("status") in {"ok", "degraded"}
    except ProcessingServiceUnavailable as exc:
        processing_status = str(exc)

    return {
        "status": "ok" if kafka_ok else "degraded",
        "kafka_producer": kafka_ok,
        "processing_service_reachable": processing_ok,
        "processing": processing_status,
    }


@app.post(
    "/api/compare",
    response_model=CompareResponse,
    responses={
        400: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    tags=["Compare"],
    summary="Разбить два файла на чанки и отправить в Kafka",
)
async def compare(
    file1: UploadFile = File(..., description="Первый файл (.pdf или .docx)"),
    file2: UploadFile = File(..., description="Второй файл (.pdf или .docx)"),
) -> CompareResponse:
    content1, name1 = await _read_upload(file1, "file1")
    content2, name2 = await _read_upload(file2, "file2")

    try:
        prepared1 = prepare_file(content1, name1)
        prepared2 = prepare_file(content2, name2)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    except PdfConversionError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "Ошибка конвертации PDF", "hint": str(exc)},
        ) from exc

    job_id = str(uuid.uuid4())
    logger.info(
        "[Compare] job_id=%s | file1=%s (%s) | file2=%s (%s)",
        job_id,
        name1,
        prepared1.format,
        name2,
        prepared2.format,
    )
    build_result = build_raw_chunk_messages(job_id, prepared1, prepared2)
    logger.info(
        "[Compare] job_id=%s | чанков: total=%s file1=%s file2=%s",
        job_id,
        len(build_result.messages),
        build_result.chunks1,
        build_result.chunks2,
    )

    try:
        await publish_raw_chunks(job_id, build_result.messages)
    except KafkaProducerError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Не удалось опубликовать задачу в Kafka",
                "hint": str(exc),
            },
        ) from exc

    ws_url = settings.job_websocket_url(job_id)
    logger.info(
        "[Compare ✓] job_id=%s queued | ws=%s",
        job_id,
        ws_url,
    )

    return CompareResponse(
        job_id=job_id,
        status="queued",
        total_chunks=len(build_result.messages),
        kafka_topic=settings.kafka_topic_raw_chunks,
        websocket_url=ws_url,
        file1=FileChunkStats(
            filename=name1,
            format=prepared1.format,
            chunks=build_result.chunks1,
        ),
        file2=FileChunkStats(
            filename=name2,
            format=prepared2.format,
            chunks=build_result.chunks2,
        ),
    )


@app.websocket("/ws/jobs/{job_id}")
async def job_websocket(websocket: WebSocket, job_id: str) -> None:
    """
    WebSocket для фронтенда: проксирует события status/result/error
    от Processing Service (:5001).
    """
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
async def result(body: ResultRequest) -> ResultResponse:
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
