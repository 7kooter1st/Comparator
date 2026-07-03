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
