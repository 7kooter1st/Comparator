from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from asyncpg.exceptions import UniqueViolationError

from app.db import Database
from app.deps import get_db, require_admin
from app.models import AuthUser

router = APIRouter(prefix="/api/admin/users", tags=["Admin"])


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(default="user", pattern=r"^(admin|user)$")


class PatchUserRequest(BaseModel):
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


class AdminUserResponse(BaseModel):
    id: UUID
    username: str
    role: str
    is_active: bool
    created_at: datetime | None = None


def _to_response(row: dict) -> AdminUserResponse:
    return AdminUserResponse(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=row["is_active"],
        created_at=row.get("created_at"),
    )


@router.get("", response_model=list[AdminUserResponse])
async def list_users(
    _admin: AuthUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> list[AdminUserResponse]:
    return [_to_response(row) for row in await db.list_users()]


@router.post("", response_model=AdminUserResponse)
async def create_user(
    body: CreateUserRequest,
    admin: AuthUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> AdminUserResponse:
    existing = await db.get_user_by_username(body.username)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Пользователь уже существует")
    try:
        row = await db.create_user(
            username=body.username,
            password=body.password,
            role=body.role,
            created_by=admin.id,
        )
    except UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail="Пользователь уже существует",
        ) from exc
    return _to_response(row)


@router.patch("/{user_id}", response_model=AdminUserResponse)
async def patch_user(
    user_id: UUID,
    body: PatchUserRequest,
    admin: AuthUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> AdminUserResponse:
    if body.is_active is None and body.password is None:
        raise HTTPException(status_code=400, detail="Нет полей для обновления")
    if user_id == admin.id and body.is_active is False:
        raise HTTPException(
            status_code=400,
            detail="Нельзя отключить собственную учётную запись",
        )
    row = await db.update_user(
        user_id,
        is_active=body.is_active,
        password=body.password,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if body.is_active is False or body.password:
        await db.revoke_user_sessions(user_id)
    return _to_response(row)
