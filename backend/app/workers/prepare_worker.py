from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from app.config import settings
from app.services.files import PdfConversionError, detect_format, pdf_to_images
from app.services.object_store import StoredObject, get_object_store
from app.workflow.envelope import command_envelope
from app.workflow.repository import WorkflowRepository

logger = logging.getLogger(__name__)

_COM_SLOT = asyncio.Semaphore(1)


class PrepareWorker:
    def __init__(self, repo: WorkflowRepository, pool) -> None:
        self._repo = repo
        self._pool = pool
        self._store = get_object_store()
        self._task: asyncio.Task | None = None
        self._running = False
        self._owner = settings.worker_id or f"gateway-{os.getpid()}"

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("[PREPARE] worker started owner=%s", self._owner)

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
                item = await self._repo.lease_work_item(
                    stage="prepare_word",
                    owner=self._owner,
                    lease_seconds=settings.lease_seconds,
                )
                if item is None:
                    item = await self._repo.lease_work_item(
                        stage="prepare_pdf",
                        owner=self._owner,
                        lease_seconds=settings.lease_seconds,
                    )
                if item is None:
                    await asyncio.sleep(settings.work_item_poll_interval_sec)
                    continue
                try:
                    await self._process(item)
                    await self._repo.complete_work_item(
                        work_item_id=str(item["id"]),
                        lease_token=str(item["lease_token"]),
                        lease_epoch=int(item["lease_epoch"]),
                        outcome="succeeded",
                    )
                except Exception as exc:
                    logger.exception("[PREPARE] failed job=%s", item.get("job_id"))
                    retried = await self._repo.retry_work_item(
                        work_item_id=str(item["id"]),
                        lease_token=str(item["lease_token"]),
                        lease_epoch=int(item["lease_epoch"]),
                        delay_seconds=min(60, 2 ** int(item["attempt"])),
                        error=str(exc),
                    )
                    if not retried:
                        await self._fail_job(str(item["job_id"]), str(exc))
        except asyncio.CancelledError:
            logger.info("[PREPARE] worker cancelled")

    async def _fail_job(self, job_id: str, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE comparison_jobs
                SET status = CASE
                        WHEN status IN ('completed', 'cancelled', 'deleted') THEN status
                        ELSE 'failed'
                    END,
                    last_message = $2,
                    failure_code = 'prepare_failed',
                    failed_at = COALESCE(failed_at, NOW()),
                    state_version = state_version + 1,
                    updated_at = NOW()
                WHERE job_id = $1
                """,
                job_id,
                f"Ошибка подготовки документов: {error}"[:500],
            )

    async def _process(self, item: dict) -> None:
        payload = item["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        inner = payload.get("payload") or payload
        job_id = str(item["job_id"])
        side = int(inner.get("side") or item.get("side") or 1)
        filename = str(inner["filename"])
        object_key = str(inner["object_key"])
        data = await self._store.get_bytes(object_key)
        fmt = detect_format(filename)
        if fmt == "docx":
            async with _COM_SLOT:
                pages = await asyncio.wait_for(
                    asyncio.to_thread(self._prepare_word_subprocess, data, filename),
                    timeout=settings.word_timeout_seconds,
                )
            await self._store_text_pages(job_id, side, filename, pages)
        else:
            images = await asyncio.wait_for(
                asyncio.to_thread(pdf_to_images, data),
                timeout=settings.prepare_timeout_seconds,
            )
            await self._store_image_pages(job_id, side, filename, images)
        await self._maybe_enqueue_ocr(job_id)

    def _prepare_word_subprocess(self, data: bytes, filename: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.docx"
            out = Path(tmp) / "out.json"
            src.write_bytes(data)
            cmd = [
                sys.executable,
                "-m",
                "app.workers.word_job",
                "--input",
                str(src),
                "--name",
                filename,
                "--output",
                str(out),
            ]
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    timeout=settings.word_timeout_seconds,
                    cwd=str(Path(__file__).resolve().parents[2]),
                )
            except subprocess.TimeoutExpired as exc:
                if exc.pid:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(exc.pid)],
                        check=False,
                        capture_output=True,
                    )
                raise
            parsed = json.loads(out.read_text(encoding="utf-8"))
            return list(parsed.get("text_pages") or [parsed.get("text") or ""])

    async def _store_text_pages(
        self,
        job_id: str,
        side: int,
        filename: str,
        pages: list[str],
    ) -> None:
        async with self._pool.acquire() as conn:
            for index, text in enumerate(pages, start=1):
                encoded = text.encode("utf-8")
                stored = StoredObject(
                    key="",
                    sha256=hashlib.sha256(encoded).hexdigest(),
                    size_bytes=len(encoded),
                    content_type="text/plain; charset=utf-8",
                )
                await self._insert_page(
                    conn,
                    job_id=job_id,
                    side=side,
                    page_number=index,
                    stored=stored,
                    filename=filename,
                    fmt="docx",
                    page_text=text,
                )

    async def _store_image_pages(
        self,
        job_id: str,
        side: int,
        filename: str,
        images: list[bytes],
    ) -> None:
        async with self._pool.acquire() as conn:
            for index, image in enumerate(images, start=1):
                key = f"jobs/{job_id}/pages/side{side}-page{index}.png"
                stored = await self._store.put_bytes(key, image, content_type="image/png")
                await self._insert_page(
                    conn,
                    job_id=job_id,
                    side=side,
                    page_number=index,
                    stored=stored,
                    filename=filename,
                    fmt="pdf",
                )

    async def _insert_page(
        self,
        conn,
        *,
        job_id: str,
        side: int,
        page_number: int,
        stored,
        filename: str,
        fmt: str,
        page_text: str | None = None,
    ) -> None:
        object_key = stored.key or None
        if object_key:
            await conn.execute(
                """
                INSERT INTO object_assets (
                    id, job_id, kind, side, page_number, object_key, sha256,
                    size_bytes, content_type
                )
                VALUES ($1, $2, 'page', $3, $4, $5, $6, $7, $8)
                ON CONFLICT (object_key) DO NOTHING
                """,
                uuid.uuid4(),
                job_id,
                side,
                page_number,
                object_key,
                stored.sha256,
                stored.size_bytes,
                stored.content_type,
            )
        await conn.execute(
            """
            INSERT INTO document_pages (
                job_id, side, page_number, object_key, sha256,
                content_type, filename, format, page_text
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (job_id, side, page_number) DO UPDATE SET
                object_key = EXCLUDED.object_key,
                sha256 = EXCLUDED.sha256,
                content_type = EXCLUDED.content_type,
                page_text = EXCLUDED.page_text
            """,
            job_id,
            side,
            page_number,
            object_key,
            stored.sha256,
            stored.content_type,
            filename,
            fmt,
            page_text,
        )

    async def _maybe_enqueue_ocr(self, job_id: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                sides = await conn.fetch(
                    """
                    SELECT side, COUNT(*) AS pages
                    FROM document_pages
                    WHERE job_id = $1
                    GROUP BY side
                    """,
                    job_id,
                )
                if {row["side"] for row in sides} != {1, 2}:
                    return
                existing_ocr = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM work_items
                    WHERE job_id = $1 AND stage = 'ocr'
                    """,
                    job_id,
                )
                if existing_ocr:
                    return
                counts = {row["side"]: int(row["pages"]) for row in sides}
                total = max(counts.values())
                pages = await conn.fetch(
                    """
                    SELECT side, page_number, object_key, sha256, content_type,
                           filename, format, page_text
                    FROM document_pages
                    WHERE job_id = $1
                    ORDER BY side, page_number
                    """,
                    job_id,
                )
                by_side: dict[int, dict[int, dict]] = {1: {}, 2: {}}
                for row in pages:
                    by_side[row["side"]][row["page_number"]] = dict(row)
                for index in range(1, total + 1):
                    task_id = f"{job_id}:ocr:{index}"
                    payload = command_envelope(
                        job_id=job_id,
                        task_id=task_id,
                        stage="ocr",
                        payload={
                            "job_id": job_id,
                            "document_id": job_id,
                            "chunk_index": index,
                            "total_chunks": total,
                            "file1": _page_ref(by_side[1].get(index)),
                            "file2": _page_ref(by_side[2].get(index)),
                        },
                        pipeline_version=settings.pipeline_version,
                    )
                    await self._repo.create_work_item(
                        conn,
                        job_id=job_id,
                        stage="ocr",
                        task_id=task_id,
                        payload=payload,
                        chunk_index=index,
                    )
                    await self._repo.enqueue_outbox(
                        conn,
                        job_id=job_id,
                        topic=settings.kafka_topic_ocr_cmd,
                        key=job_id,
                        payload=payload,
                    )
                await conn.execute(
                    """
                    UPDATE comparison_jobs
                    SET total_chunks = $2,
                        status = CASE
                            WHEN status IN ('completed', 'failed', 'cancelled', 'deleted')
                            THEN status
                            ELSE 'queued'
                        END,
                        last_message = 'Процесс в очереди',
                        queued_at = COALESCE(queued_at, NOW()),
                        state_version = state_version + 1,
                        updated_at = NOW()
                    WHERE job_id = $1
                    """,
                    job_id,
                    total,
                )


def _page_ref(row: dict | None) -> dict | None:
    if row is None:
        return None
    if row.get("page_text") is not None and not row.get("object_key"):
        return {
            "content": row["page_text"],
            "content_type": "text",
            "filename": row["filename"],
            "format": row["format"],
        }
    return {
        "object_key": row["object_key"],
        "sha256": row["sha256"],
        "content_type": row["content_type"],
        "filename": row["filename"],
        "format": row["format"],
    }
