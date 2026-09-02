from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.db import Database
from app.deps import get_current_user, get_db, get_session_token
from app.models import AuthUser
from app.security import new_session_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class UserResponse(BaseModel):
    id: UUID
    username: str
    role: str
    is_active: bool = True
    created_at: datetime | None = None


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_days * 86400,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
    )


def _user_response(row: dict) -> UserResponse:
    return UserResponse(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=row.get("is_active", True),
        created_at=row.get("created_at"),
    )


@router.post("/login", response_model=UserResponse)
async def login(
    body: LoginRequest,
    response: Response,
    db: Database = Depends(get_db),
) -> UserResponse:
    user = await db.get_user_by_username(body.username.strip())
    if user is None or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if not user["is_active"]:
        raise HTTPException(status_code=401, detail="Учётная запись отключена")

    token = new_session_token()
    await db.create_session(user["id"], token)
    _set_session_cookie(response, token)
    return _user_response(user)


@router.post("/logout")
async def logout(
    response: Response,
    db: Database = Depends(get_db),
    token: str | None = Depends(get_session_token),
) -> dict[str, bool]:
    if token:
        await db.revoke_session(token)
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=UserResponse)
async def me(user: AuthUser = Depends(get_current_user)) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
    )


@router.post("/password", response_model=UserResponse)
async def change_password(
    body: PasswordChangeRequest,
    db: Database = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
    token: str | None = Depends(get_session_token),
) -> UserResponse:
    stored = await db.get_user_by_username(user.username)
    if stored is None or not verify_password(
        body.current_password,
        stored["password_hash"],
    ):
        raise HTTPException(status_code=401, detail="Неверный текущий пароль")
    await db.set_password(user.id, body.new_password)
    await db.revoke_user_sessions(user.id, except_token=token)
    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
    )
