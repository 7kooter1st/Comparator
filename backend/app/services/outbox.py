from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiokafka import AIOKafkaProducer

from app.config import settings
from app.workflow.repository import WorkflowRepository

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(self, repo: WorkflowRepository, producer: AIOKafkaProducer) -> None:
        self._repo = repo
        self._producer = producer
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("[OUTBOX] publisher started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        try:
            while self._running:
                rows = await self._repo.claim_unpublished_outbox()
                if not rows:
                    await asyncio.sleep(settings.outbox_poll_interval_sec)
                    continue
                for row in rows:
                    await self._publish_one(row)
        except asyncio.CancelledError:
            logger.info("[OUTBOX] publisher cancelled")

    async def _publish_one(self, row: dict[str, Any]) -> None:
        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        key = (row["message_key"] or "").encode("utf-8")
        try:
            await self._producer.send_and_wait(
                row["topic"],
                value=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                key=key or None,
            )
            await self._repo.mark_outbox_published(str(row["id"]))
        except Exception as exc:
            logger.exception("[OUTBOX] publish failed id=%s", row["id"])
            await self._repo.mark_outbox_error(str(row["id"]), str(exc))
            await asyncio.sleep(1)
