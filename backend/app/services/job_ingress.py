from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

import asyncpg
from fastapi import HTTPException

from app.config import settings
from app.workflow.envelope import command_envelope
from app.workflow.repository import WorkflowRepository

logger = logging.getLogger(__name__)


class DurableJobIngress:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.workflow = WorkflowRepository(pool)

    async def lookup_idempotency(
        self,
        user_id: uuid.UUID,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT request_hash, response_json
                FROM idempotency_requests
                WHERE user_id = $1 AND idempotency_key = $2
                """,
                user_id,
                idempotency_key,
            )
        if row is None:
            return None
        payload = row["response_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload["_request_hash"] = row["request_hash"]
        return payload

    async def create_or_resume(
        self,
        *,
        user_id: uuid.UUID,
        file1_name: str,
        file2_name: str,
        object1: dict[str, Any],
        object2: dict[str, Any],
        idempotency_key: str | None,
        job_id: str,
    ) -> tuple[dict[str, Any], bool]:
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "user_id": str(user_id),
                    "file1": object1["sha256"],
                    "file2": object2["sha256"],
                    "file1_name": file1_name,
                    "file2_name": file2_name,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if idempotency_key:
                    existing = await conn.fetchrow(
                        """
                        SELECT request_hash, response_json
                        FROM idempotency_requests
                        WHERE user_id = $1 AND idempotency_key = $2
                        """,
                        user_id,
                        idempotency_key,
                    )
                    if existing is not None:
                        if existing["request_hash"] != request_hash:
                            raise HTTPException(
                                status_code=409,
                                detail={
                                    "error": "Idempotency-Key уже использован с другим запросом",
                                },
                            )
                        payload = existing["response_json"]
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        return payload, False

                await conn.execute(
                    """
                    INSERT INTO comparison_jobs (
                        job_id, document_id, user_id, file1_name, file2_name,
                        status, last_message, pipeline_version, queued_at, preparing_at
                    )
                    VALUES (
                        $1, $1, $2, $3, $4,
                        'preparing', 'Подготовка документов…', $5, NOW(), NOW()
                    )
                    """,
                    job_id,
                    user_id,
                    file1_name,
                    file2_name,
                    settings.pipeline_version,
                )
                for side, obj, name in ((1, object1, file1_name), (2, object2, file2_name)):
                    await conn.execute(
                        """
                        INSERT INTO object_assets (
                            id, job_id, kind, side, object_key, sha256,
                            size_bytes, content_type
                        )
                        VALUES ($1, $2, 'original', $3, $4, $5, $6, $7)
                        """,
                        uuid.uuid4(),
                        job_id,
                        side,
                        obj["key"],
                        obj["sha256"],
                        obj["size_bytes"],
                        obj["content_type"],
                    )
                    await conn.execute(
                        """
                        INSERT INTO job_files (
                            id, job_id, side, original_filename, content_type,
                            size_bytes, sha256, storage_path
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (job_id, side) DO UPDATE SET
                            original_filename = EXCLUDED.original_filename,
                            content_type = EXCLUDED.content_type,
                            size_bytes = EXCLUDED.size_bytes,
                            sha256 = EXCLUDED.sha256,
                            storage_path = EXCLUDED.storage_path
                        """,
                        uuid.uuid4(),
                        job_id,
                        side,
                        name,
                        obj["content_type"],
                        obj["size_bytes"],
                        obj["sha256"],
                        obj["key"],
                    )
                    stage = "prepare_word" if name.lower().endswith(".docx") else "prepare_pdf"
                    topic = (
                        settings.kafka_topic_prepare_word
                        if stage == "prepare_word"
                        else settings.kafka_topic_prepare_pdf
                    )
                    task_id = f"{job_id}:{stage}:{side}"
                    payload = command_envelope(
                        job_id=job_id,
                        task_id=task_id,
                        stage=stage,
                        payload={
                            "side": side,
                            "filename": name,
                            "object_key": obj["key"],
                            "sha256": obj["sha256"],
                            "content_type": obj["content_type"],
                        },
                        pipeline_version=settings.pipeline_version,
                    )
                    await self.workflow.create_work_item(
                        conn,
                        job_id=job_id,
                        stage=stage,
                        task_id=task_id,
                        payload=payload,
                        side=side,
                    )
                    await self.workflow.enqueue_outbox(
                        conn,
                        job_id=job_id,
                        topic=topic,
                        key=task_id,
                        payload=payload,
                    )

                await self.workflow.append_job_event(
                    conn,
                    job_id=job_id,
                    event_type="job.accepted",
                    payload={"file1": file1_name, "file2": file2_name},
                )
                response = {
                    "job_id": job_id,
                    "status": "preparing",
                    "total_chunks": 0,
                    "kafka_topic": settings.kafka_topic_ocr_cmd,
                    "websocket_url": settings.job_websocket_url(job_id),
                    "file1": {"filename": file1_name, "format": "pending", "chunks": 0},
                    "file2": {"filename": file2_name, "format": "pending", "chunks": 0},
                }
                if idempotency_key:
                    await conn.execute(
                        """
                        INSERT INTO idempotency_requests (
                            user_id, idempotency_key, request_hash, job_id, response_json
                        )
                        VALUES ($1, $2, $3, $4, $5::jsonb)
                        """,
                        user_id,
                        idempotency_key,
                        request_hash,
                        job_id,
                        json.dumps(response, ensure_ascii=False),
                    )
                return response, True
