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

# How long to retry 404 JobNotFound before giving up (Processing may not
# have registered the job yet while Kafka chunks are still in flight).
_JOB_NOT_FOUND_GRACE_SEC = 60.0


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


def _is_result_payload(message: str) -> bool:
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return False
    return parsed.get("type") == "result" and isinstance(parsed.get("data"), dict)


async def _relay_upstream_ws(
    job_id: str,
    client_ws: WebSocket,
    send_lock: asyncio.Lock,
    done: asyncio.Event,
) -> None:
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
                if done.is_set():
                    break
                _log_ws_event("Processing→Gateway", job_id, message)
                async with send_lock:
                    if client_ws.client_state == WebSocketState.CONNECTED:
                        await client_ws.send_text(message)
                        _log_ws_event("Gateway→Frontend", job_id, message)
                if _is_result_payload(message):
                    logger.info("[WS ✓] job_id=%s: result получен с upstream", job_id)
                    done.set()
                    break

        async def forward_client() -> None:
            while not done.is_set():
                if client_ws.client_state != WebSocketState.CONNECTED:
                    break
                try:
                    data = await client_ws.receive_text()
                except WebSocketDisconnect:
                    logger.info("[WS] job_id=%s: frontend отключился", job_id)
                    done.set()
                    break
                if data.strip().lower() not in {"ping", "pong"}:
                    logger.info("[WS Frontend→Gateway] job_id=%s | %s", job_id, data[:200])
                await upstream.send(data)

        forward_task = asyncio.create_task(forward_upstream())
        client_task = asyncio.create_task(forward_client())
        done_task = asyncio.create_task(done.wait())
        await asyncio.wait(
            {forward_task, client_task, done_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in (forward_task, client_task, done_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in (forward_task, client_task):
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    raise exc


async def _poll_processing_api(
    job_id: str,
    client_ws: WebSocket,
    send_lock: asyncio.Lock,
    done: asyncio.Event,
) -> None:
    logger.info(
        "[Polling] job_id=%s: параллельный REST polling каждые %ss",
        job_id,
        settings.processing_poll_interval_sec,
    )
    last_signature: tuple[int, str] | None = None
    poll_count = 0
    not_found_since: float | None = None

    while not done.is_set() and client_ws.client_state == WebSocketState.CONNECTED:
        poll_count += 1
        logger.info("[Polling] job_id=%s попытка #%s", job_id, poll_count)

        # Prefer result endpoint — Aggregator may already have finalized JSON
        # even if status relay never reached the frontend over WS.
        try:
            result = await get_job_result(job_id)
        except ResultNotReady:
            result = None
        except ProcessingServiceUnavailable as exc:
            logger.warning("[Polling] job_id=%s result unavailable: %s", job_id, exc)
            result = None

        if result is not None:
            event = _result_event(job_id, result)
            async with send_lock:
                if client_ws.client_state == WebSocketState.CONNECTED and not done.is_set():
                    _log_ws_event("Polling→Frontend", job_id, event)
                    await client_ws.send_text(event)
                    logger.info("[Polling ✓] job_id=%s: результат отправлен на frontend", job_id)
                    done.set()
            return

        try:
            status = await get_job_status(job_id)
            not_found_since = None
        except JobNotFound:
            now = asyncio.get_running_loop().time()
            if not_found_since is None:
                not_found_since = now
            elif now - not_found_since >= _JOB_NOT_FOUND_GRACE_SEC:
                msg = _error_event(job_id, "Задача не найдена в Processing Service")
                logger.error("[Polling → Frontend] job_id=%s | задача не найдена (timeout)", job_id)
                async with send_lock:
                    if client_ws.client_state == WebSocketState.CONNECTED:
                        await client_ws.send_text(msg)
                done.set()
                return
            logger.info(
                "[Polling] job_id=%s: job ещё не в StateManager, ждём…",
                job_id,
            )
            await asyncio.sleep(settings.processing_poll_interval_sec)
            continue
        except ProcessingServiceUnavailable as exc:
            logger.warning("[Polling] job_id=%s status unavailable: %s", job_id, exc)
            await asyncio.sleep(settings.processing_poll_interval_sec)
            continue

        signature = (status.get("processed_chunks", 0), status.get("status", ""))
        if signature != last_signature:
            event = _status_event(job_id, status)
            async with send_lock:
                if client_ws.client_state == WebSocketState.CONNECTED and not done.is_set():
                    _log_ws_event("Polling→Frontend", job_id, event)
                    await client_ws.send_text(event)
            last_signature = signature

        job_status = status.get("status")
        if job_status == "failed":
            msg = _error_event(job_id, status.get("message", "Ошибка обработки"))
            logger.error("[Polling → Frontend] job_id=%s | failed", job_id)
            async with send_lock:
                if client_ws.client_state == WebSocketState.CONNECTED:
                    await client_ws.send_text(msg)
            done.set()
            return

        await asyncio.sleep(settings.processing_poll_interval_sec)


async def relay_job_to_client(job_id: str, client_ws: WebSocket) -> None:
    """Relay Processing events to frontend via upstream WS + parallel REST polling."""
    await client_ws.accept()
    send_lock = asyncio.Lock()
    done = asyncio.Event()

    async def ws_task() -> None:
        try:
            await _relay_upstream_ws(job_id, client_ws, send_lock, done)
        except Exception as exc:
            logger.warning(
                "[WS] job_id=%s: upstream ошибка (%s: %s) — продолжаем REST polling",
                job_id,
                type(exc).__name__,
                exc,
            )

    async def poll_task() -> None:
        try:
            await _poll_processing_api(job_id, client_ws, send_lock, done)
        except Exception:
            logger.exception("[Polling] job_id=%s: сбой polling", job_id)

    ws = asyncio.create_task(ws_task(), name=f"ws-relay-{job_id}")
    poll = asyncio.create_task(poll_task(), name=f"poll-relay-{job_id}")

    try:
        await done.wait()
    except asyncio.CancelledError:
        done.set()
        raise
    finally:
        done.set()
        for task in (ws, poll):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("[WS] job_id=%s: relay завершён", job_id)
