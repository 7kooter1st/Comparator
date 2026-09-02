import hashlib
import logging
import shutil
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".pdf", ".docx"}:
        return suffix
    return ".bin"


def job_dir(user_id: str, job_id: str) -> Path:
    return settings.upload_dir / str(user_id) / job_id


def save_original(
    *,
    user_id: str,
    job_id: str,
    side: int,
    filename: str,
    content: bytes,
    content_type: str,
) -> dict:
    directory = job_dir(user_id, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    stored_name = f"file{side}{_safe_suffix(filename)}"
    path = directory / stored_name
    path.write_bytes(content)
    relative = path.relative_to(settings.upload_dir).as_posix()
    digest = hashlib.sha256(content).hexdigest()
    return {
        "side": side,
        "original_filename": filename,
        "content_type": content_type or "application/octet-stream",
        "size_bytes": len(content),
        "sha256": digest,
        "storage_path": relative,
        "absolute_path": path,
    }


def resolve_stored_path(storage_path: str) -> Path:
    relative = Path(storage_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid storage path")
    path = (settings.upload_dir / relative).resolve()
    uploads = settings.upload_dir.resolve()
    if uploads not in path.parents and path != uploads:
        raise ValueError("Invalid storage path")
    return path


def delete_job_files(user_id: str, job_id: str) -> None:
    directory = job_dir(user_id, job_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
        parent = directory.parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
        logger.info("[FILES] removed job_id=%s", job_id)
