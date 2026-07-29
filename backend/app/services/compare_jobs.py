import asyncio
import logging

from app.services.chunk_builder import build_raw_chunk_messages
from app.services.files import (
    DocxConversionError,
    PdfConversionError,
    prepare_file,
)
from app.services.kafka_producer import KafkaProducerError, publish_raw_chunks

logger = logging.getLogger(__name__)

_prepare_errors: dict[str, str] = {}


def get_prepare_error(job_id: str) -> str | None:
    return _prepare_errors.pop(job_id, None)


def peek_prepare_error(job_id: str) -> str | None:
    return _prepare_errors.get(job_id)


async def run_compare_pipeline(
    job_id: str,
    content1: bytes,
    name1: str,
    content2: bytes,
    name2: str,
) -> None:
    """Chunking + Kafka in background so HTTP can return before Cloudflare times out."""
    _prepare_errors.pop(job_id, None)
    logger.info(
        "[Compare BG] start job_id=%s | file1=%s | file2=%s",
        job_id,
        name1,
        name2,
    )
    try:
        # Word COM is STA — never prepare two DOCX files in parallel threads.
        prepared1 = await asyncio.to_thread(prepare_file, content1, name1)
        prepared2 = await asyncio.to_thread(prepare_file, content2, name2)
    except (ValueError, PdfConversionError, DocxConversionError) as exc:
        _prepare_errors[job_id] = str(exc)
        logger.error("[Compare BG ✗] job_id=%s prepare failed: %s", job_id, exc)
        return
    except Exception as exc:
        _prepare_errors[job_id] = f"Ошибка подготовки файлов: {exc}"
        logger.exception("[Compare BG ✗] job_id=%s prepare failed", job_id)
        return

    build_result = build_raw_chunk_messages(job_id, prepared1, prepared2)
    logger.info(
        "[Compare BG] job_id=%s | чанков: total=%s file1=%s file2=%s",
        job_id,
        len(build_result.messages),
        build_result.chunks1,
        build_result.chunks2,
    )

    try:
        await publish_raw_chunks(job_id, build_result.messages)
    except KafkaProducerError as exc:
        _prepare_errors[job_id] = str(exc)
        logger.error("[Compare BG ✗] job_id=%s kafka failed: %s", job_id, exc)
        return

    logger.info("[Compare BG ✓] job_id=%s queued in Kafka", job_id)
