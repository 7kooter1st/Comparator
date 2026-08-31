import asyncio
import logging
from typing import Literal

from app.services.chunk_builder import build_raw_chunk_messages
from app.services.files import (
    DocxConversionError,
    PdfConversionError,
    prepare_file,
)
from app.services.kafka_producer import KafkaProducerError, publish_raw_chunks
from app.services.processing_client import register_job

logger = logging.getLogger(__name__)

PipelinePhase = Literal["preparing", "queued", "failed"]

_prepare_errors: dict[str, str] = {}
_pipeline_phase: dict[str, PipelinePhase] = {}


def get_prepare_error(job_id: str) -> str | None:
    return _prepare_errors.pop(job_id, None)


def peek_prepare_error(job_id: str) -> str | None:
    return _prepare_errors.get(job_id)


def get_pipeline_phase(job_id: str) -> PipelinePhase | None:
    return _pipeline_phase.get(job_id)


def mark_job_accepted(job_id: str) -> None:
    """Mark job as preparing as soon as HTTP compare is accepted."""
    _prepare_errors.pop(job_id, None)
    _set_phase(job_id, "preparing")


def _set_phase(job_id: str, phase: PipelinePhase) -> None:
    _pipeline_phase[job_id] = phase


async def run_compare_pipeline(
    job_id: str,
    content1: bytes,
    name1: str,
    content2: bytes,
    name2: str,
) -> None:
    """Chunking + Kafka in background so HTTP can return before Cloudflare times out."""
    _prepare_errors.pop(job_id, None)
    _set_phase(job_id, "preparing")
    logger.info(
        "[Compare BG] start job_id=%s | file1=%s | file2=%s",
        job_id,
        name1,
        name2,
    )

    await register_job(
        job_id,
        status="preparing",
        message="Подготовка документов (Chunking)...",
    )

    try:
        # Word COM is STA — never prepare two DOCX files in parallel threads.
        prepared1 = await asyncio.to_thread(prepare_file, content1, name1)
        prepared2 = await asyncio.to_thread(prepare_file, content2, name2)
    except (ValueError, PdfConversionError, DocxConversionError) as exc:
        _prepare_errors[job_id] = str(exc)
        _set_phase(job_id, "failed")
        logger.error("[Compare BG ✗] job_id=%s prepare failed: %s", job_id, exc)
        await register_job(
            job_id,
            status="failed",
            message=f"Ошибка подготовки документов: {exc}",
        )
        return
    except Exception as exc:
        _prepare_errors[job_id] = f"Ошибка подготовки файлов: {exc}"
        _set_phase(job_id, "failed")
        logger.exception("[Compare BG ✗] job_id=%s prepare failed", job_id)
        await register_job(
            job_id,
            status="failed",
            message=f"Ошибка подготовки файлов: {exc}",
        )
        return

    build_result = build_raw_chunk_messages(job_id, prepared1, prepared2)
    logger.info(
        "[Compare BG] job_id=%s | чанков: total=%s file1=%s file2=%s",
        job_id,
        len(build_result.messages),
        build_result.chunks1,
        build_result.chunks2,
    )

    await register_job(
        job_id,
        total_chunks=len(build_result.messages),
        status="queued",
        message="Публикация чанков в Kafka...",
    )

    try:
        await publish_raw_chunks(job_id, build_result.messages)
    except KafkaProducerError as exc:
        _prepare_errors[job_id] = str(exc)
        _set_phase(job_id, "failed")
        logger.error("[Compare BG ✗] job_id=%s kafka failed: %s", job_id, exc)
        await register_job(
            job_id,
            total_chunks=len(build_result.messages),
            status="failed",
            message=f"Ошибка Kafka: {exc}",
        )
        return

    _set_phase(job_id, "queued")
    await register_job(
        job_id,
        total_chunks=len(build_result.messages),
        status="queued",
        message="Чанки в Kafka, ожидание Processing...",
    )
    logger.info("[Compare BG ✓] job_id=%s queued in Kafka", job_id)
