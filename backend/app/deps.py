from fastapi import Cookie, Depends, HTTPException, Request, WebSocket

from app.config import settings
from app.db import Database, database
from app.models import AuthUser


def get_db() -> Database:
    return database


async def get_optional_user(
    request: Request,
    db: Database = Depends(get_db),
    session_token: str | None = Cookie(
        default=None,
        alias=settings.session_cookie_name,
    ),
) -> AuthUser | None:
    token = session_token or request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    row = await db.get_session_user(token)
    if row is None:
        return None
    return AuthUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=row["is_active"],
        session_id=row["session_id"],
    )


async def get_current_user(
    user: AuthUser | None = Depends(get_optional_user),
) -> AuthUser:
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


async def require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


def get_session_token(request: Request) -> str | None:
    return request.cookies.get(settings.session_cookie_name)


async def authenticate_websocket(
    websocket: WebSocket,
    db: Database,
) -> AuthUser | None:
    token = websocket.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    row = await db.get_session_user(token)
    if row is None:
        return None
    return AuthUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=row["is_active"],
        session_id=row["session_id"],
    )
