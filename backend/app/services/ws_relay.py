import asyncio
import contextlib
import json
import logging

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.config import settings
from app.logging_config import format_payload_for_log
from app.services.processing_client import (
    JobNotFound,
    ProcessingServiceUnavailable,
    ResultNotReady,
    get_job_result,
    get_job_status,
)

logger = logging.getLogger(__name__)


def _status_event(job_id: str, data: dict) -> str:
    return json.dumps(
        {"type": "status", "job_id": job_id, "data": data},
        ensure_ascii=False,
    )


def _result_event(job_id: str, data: dict) -> str:
    return json.dumps(
        {"type": "result", "job_id": job_id, "data": data},
        ensure_ascii=False,
    )


def _error_event(job_id: str, message: str, details: dict | None = None) -> str:
    payload: dict = {"message": message}
    if details:
        payload["details"] = details
    return json.dumps(
        {"type": "error", "job_id": job_id, "data": payload},
        ensure_ascii=False,
    )


def _log_ws_event(direction: str, job_id: str, message: str) -> None:
    try:
        parsed = json.loads(message)
        event_type = parsed.get("type", "?")
        logger.info(
            "[WS %s] job_id=%s type=%s | %s",
            direction,
            job_id,
            event_type,
            format_payload_for_log(parsed),
        )
    except json.JSONDecodeError:
        logger.info("[WS %s] job_id=%s | %s", direction, job_id, message[:200])


async def _relay_upstream_ws(job_id: str, client_ws: WebSocket) -> None:
    upstream_url = f"{settings.processing_ws_base}/ws/jobs/{job_id}"
    logger.info("[WS] job_id=%s: подключение frontend принято", job_id)
    logger.info("[WS → Processing] job_id=%s: upstream %s", job_id, upstream_url)

    async with websockets.connect(
        upstream_url,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
    ) as upstream:
        logger.info("[WS ✓] job_id=%s: upstream Processing подключён", job_id)

        async def forward_upstream() -> None:
            async for message in upstream:
                _log_ws_event("Processing→Gateway", job_id, message)
                if client_ws.client_state == WebSocketState.CONNECTED:
                    await client_ws.send_text(message)
                    _log_ws_event("Gateway→Frontend", job_id, message)

        async def forward_client() -> None:
            while True:
                if client_ws.client_state != WebSocketState.CONNECTED:
                    break
                try:
                    data = await client_ws.receive_text()
                except WebSocketDisconnect:
                    logger.info("[WS] job_id=%s: frontend отключился", job_id)
                    break
                if data.strip().lower() not in {"ping", "pong"}:
                    logger.info("[WS Frontend→Gateway] job_id=%s | %s", job_id, data[:200])
                await upstream.send(data)

        forward_task = asyncio.create_task(forward_upstream())
        client_task = asyncio.create_task(forward_client())
        done, pending = await asyncio.wait(
            {forward_task, client_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc


async def _poll_processing_api(job_id: str, client_ws: WebSocket) -> None:
    logger.warning(
        "[WS] job_id=%s: режим REST polling каждые %ss",
        job_id,
        settings.processing_poll_interval_sec,
    )
    last_signature: tuple[int, str] | None = None
    poll_count = 0

    while client_ws.client_state == WebSocketState.CONNECTED:
        poll_count += 1
        logger.info("[Polling] job_id=%s попытка #%s", job_id, poll_count)
        try:
            status = await get_job_status(job_id)
        except JobNotFound:
            msg = _error_event(job_id, "Задача не найдена в Processing Service")
            logger.error("[Polling → Frontend] job_id=%s | задача не найдена", job_id)
            await client_ws.send_text(msg)
            return
        except ProcessingServiceUnavailable as exc:
            msg = _error_event(job_id, str(exc))
            logger.error("[Polling → Frontend] job_id=%s | %s", job_id, exc)
            await client_ws.send_text(msg)
            await asyncio.sleep(settings.processing_poll_interval_sec)
            continue

        signature = (status.get("processed_chunks", 0), status.get("status", ""))
        if signature != last_signature:
            event = _status_event(job_id, status)
            _log_ws_event("Polling→Frontend", job_id, event)
            await client_ws.send_text(event)
            last_signature = signature

        job_status = status.get("status")
        if job_status == "failed":
            msg = _error_event(job_id, status.get("message", "Ошибка обработки"))
            logger.error("[Polling → Frontend] job_id=%s | failed", job_id)
            await client_ws.send_text(msg)
            return

        if job_status == "completed":
            try:
                result = await get_job_result(job_id)
            except ResultNotReady:
                await asyncio.sleep(settings.processing_poll_interval_sec)
                continue
            except ProcessingServiceUnavailable as exc:
                msg = _error_event(job_id, str(exc))
                await client_ws.send_text(msg)
                return

            event = _result_event(job_id, result)
            _log_ws_event("Polling→Frontend", job_id, event)
            await client_ws.send_text(event)
            logger.info("[Polling ✓] job_id=%s: результат отправлен на frontend", job_id)
            return

        await asyncio.sleep(settings.processing_poll_interval_sec)


async def relay_job_to_client(job_id: str, client_ws: WebSocket) -> None:
    await client_ws.accept()
    try:
        await _relay_upstream_ws(job_id, client_ws)
    except Exception as exc:
        logger.warning(
            "[WS] job_id=%s: upstream недоступен (%s: %s), переключаюсь на REST polling",
            job_id,
            type(exc).__name__,
            exc,
        )
        if client_ws.client_state == WebSocketState.CONNECTED:
            await _poll_processing_api(job_id, client_ws)
