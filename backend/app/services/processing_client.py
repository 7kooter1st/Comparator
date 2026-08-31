import asyncio
import logging

import httpx

from app.config import settings
from app.logging_config import format_payload_for_log

logger = logging.getLogger(__name__)


class ProcessingServiceError(Exception):
    pass


class ProcessingServiceUnavailable(ProcessingServiceError):
    pass


class JobNotFound(ProcessingServiceError):
    pass


class ResultNotReady(ProcessingServiceError):
    pass


def _base_url() -> str:
    return settings.processing_service_url.rstrip("/")


async def get_health() -> dict:
    url = f"{_base_url()}/health"
    logger.info("[Processing ← GET] %s", url)
    try:
        async with httpx.AsyncClient(timeout=settings.processing_request_timeout_sec) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            logger.info(
                "[Processing →] health: %s",
                format_payload_for_log(data),
            )
            return data
    except httpx.HTTPError as exc:
        logger.error("[Processing ✗] health недоступен: %s", exc)
        raise ProcessingServiceUnavailable(f"Processing Service недоступен: {exc}") from exc


async def register_job(
    job_id: str,
    *,
    total_chunks: int = 0,
    status: str = "queued",
    message: str = "Ожидание чанков из Kafka...",
    required: bool = False,
) -> dict | None:
    """Register a job before Kafka publication.

    The initial registration is required: without it Chunking would return a
    job_id that Processing does not know about. Later status refreshes remain
    best-effort because Kafka also carries the job.
    """
    url = f"{_base_url()}/api/jobs"
    payload = {
        "job_id": job_id,
        "document_id": job_id,
        "total_chunks": total_chunks,
        "status": status,
        "message": message,
    }
    logger.info("[Processing ← POST] %s job_id=%s status=%s", url, job_id, status)
    attempts = settings.processing_registration_attempts if required else 1
    attempts = max(1, attempts)
    last_error: httpx.HTTPError | None = None

    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(
                timeout=settings.processing_request_timeout_sec
            ) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
                logger.info(
                    "[Processing →] registered job_id=%s: %s",
                    job_id,
                    format_payload_for_log(data),
                )
                return data
        except httpx.HTTPError as exc:
            last_error = exc
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status_code is None or status_code >= 500
            if not retryable or attempt >= attempts:
                break

            delay = settings.processing_registration_retry_delay_sec * attempt
            logger.warning(
                "[Processing ✗] register job_id=%s failed "
                "(attempt %s/%s): %s; retry in %.1fs",
                job_id,
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    error = (
        f"Processing Service не зарегистрировал задачу {job_id}: {last_error}"
    )
    if required:
        raise ProcessingServiceUnavailable(error) from last_error

    logger.warning(
        "[Processing ✗] register job_id=%s failed (will rely on Kafka): %s",
        job_id,
        last_error,
    )
    return None


async def get_job_status(job_id: str) -> dict:
    url = f"{_base_url()}/api/jobs/{job_id}"
    logger.info("[Processing ← GET] %s", url)
    try:
        async with httpx.AsyncClient(timeout=settings.processing_request_timeout_sec) as client:
            response = await client.get(url)
            if response.status_code == 404:
                logger.warning("[Processing →] job_id=%s: 404 not found", job_id)
                raise JobNotFound(f"Job {job_id} not found")
            response.raise_for_status()
            data = response.json()
            logger.info(
                "[Processing →] job_id=%s status: %s",
                job_id,
                format_payload_for_log(data),
            )
            return data
    except httpx.HTTPError as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            logger.warning("[Processing →] job_id=%s: 404 not found", job_id)
            raise JobNotFound(f"Job {job_id} not found") from exc
        logger.error("[Processing ✗] job_id=%s status error: %s", job_id, exc)
        raise ProcessingServiceUnavailable(f"Ошибка запроса статуса: {exc}") from exc


async def get_job_result(job_id: str) -> dict:
    url = f"{_base_url()}/api/jobs/{job_id}/result"
    logger.info("[Processing ← GET] %s", url)
    try:
        async with httpx.AsyncClient(timeout=settings.processing_request_timeout_sec) as client:
            response = await client.get(url)
            if response.status_code == 404:
                logger.info("[Processing →] job_id=%s result: ещё не готов (404)", job_id)
                raise ResultNotReady(f"Result for job {job_id} is not ready yet")
            response.raise_for_status()
            data = response.json()
            logger.info(
                "[Processing →] job_id=%s result: %s",
                job_id,
                format_payload_for_log(data),
            )
            return data
    except httpx.HTTPError as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            logger.info("[Processing →] job_id=%s result: ещё не готов (404)", job_id)
            raise ResultNotReady(f"Result for job {job_id} is not ready yet") from exc
        logger.error("[Processing ✗] job_id=%s result error: %s", job_id, exc)
        raise ProcessingServiceUnavailable(f"Ошибка запроса результата: {exc}") from exc
