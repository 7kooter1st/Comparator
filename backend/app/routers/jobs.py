from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import settings
from app.db import Database
from app.deps import get_current_user, get_db
from app.models import AuthUser
from app.services.file_store import delete_job_files, resolve_stored_path
from app.services.processing_client import (
    JobNotFound,
    ProcessingServiceUnavailable,
    ResultNotReady,
    get_job_result,
    get_job_status,
)

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])


class JobListItem(BaseModel):
    job_id: str
    status: str
    file1_name: str
    file2_name: str
    processed_chunks: int
    total_chunks: int
    message: str
    verdict: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    websocket_url: str | None = None


def _to_item(row: dict[str, Any]) -> JobListItem:
    return JobListItem(
        job_id=row["job_id"],
        status=row["status"],
        file1_name=row.get("file1_name") or "",
        file2_name=row.get("file2_name") or "",
        processed_chunks=row.get("processed_chunks") or 0,
        total_chunks=row.get("total_chunks") or 0,
        message=row.get("last_message") or "",
        verdict=row.get("verdict"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        websocket_url=settings.job_websocket_url(row["job_id"]),
    )


def require_owned_job(
    job: dict[str, Any] | None,
    user: AuthUser,
    *,
    allow_admin: bool = False,
) -> dict[str, Any]:
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    owner_id = job["user_id"]
    if isinstance(owner_id, UUID):
        same_owner = owner_id == user.id
    else:
        same_owner = str(owner_id) == str(user.id)
    if same_owner:
        return job
    if allow_admin and user.is_admin:
        return job
    raise HTTPException(status_code=403, detail="Нет доступа к этой задаче")


@router.get("", response_model=list[JobListItem])
async def list_jobs(
    user: AuthUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> list[JobListItem]:
    rows = await db.list_jobs_for_user(user.id)
    return [_to_item(row) for row in rows]


@router.get("/{job_id}", response_model=JobListItem)
async def get_job(
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JobListItem:
    job = require_owned_job(await db.get_job(job_id), user)
    try:
        remote = await get_job_status(job_id)
        job = {
            **job,
            "status": remote.get("status", job["status"]),
            "processed_chunks": remote.get(
                "processed_chunks",
                job.get("processed_chunks", 0),
            ),
            "total_chunks": remote.get("total_chunks", job.get("total_chunks", 0)),
            "last_message": remote.get("message", job.get("last_message", "")),
        }
    except (JobNotFound, ProcessingServiceUnavailable):
        pass
    return _to_item(job)


@router.get("/{job_id}/result")
async def job_result(
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    require_owned_job(await db.get_job(job_id), user)
    try:
        return await get_job_result(job_id)
    except ResultNotReady as exc:
        raise HTTPException(
            status_code=404,
            detail="Результат ещё не готов",
        ) from exc
    except ProcessingServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{job_id}/files/{side}")
async def download_job_file(
    job_id: str,
    side: int,
    user: AuthUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> FileResponse:
    if side not in (1, 2):
        raise HTTPException(status_code=400, detail="side должен быть 1 или 2")
    require_owned_job(await db.get_job(job_id), user)
    stored = await db.get_job_file(job_id, side)
    if stored is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    try:
        path = resolve_stored_path(stored["storage_path"])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Файл не найден") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл отсутствует на диске")
    return FileResponse(
        path,
        filename=stored["original_filename"],
        media_type=stored["content_type"],
    )


@router.delete("/{job_id}")
async def delete_job(
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, bool]:
    job = require_owned_job(await db.get_job(job_id), user, allow_admin=True)
    delete_job_files(str(job["user_id"]), job_id)
    deleted = await db.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return {"ok": True}
