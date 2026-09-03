import logging
import uuid
from typing import Any

import asyncpg
from asyncpg.exceptions import UndefinedTableError

from app.config import settings
from app.security import hash_password, hash_session_token, session_expiry

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions(user_id);

CREATE TABLE IF NOT EXISTS comparison_jobs (
    job_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id),
    file1_name TEXT NOT NULL DEFAULT '',
    file2_name TEXT NOT NULL DEFAULT '',
    total_chunks INTEGER NOT NULL DEFAULT 0,
    processed_chunks INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    last_message TEXT NOT NULL DEFAULT '',
    comparison_claimed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comparison_jobs_user
    ON comparison_jobs(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS job_files (
    id UUID PRIMARY KEY,
    job_id TEXT NOT NULL
        REFERENCES comparison_jobs(job_id) ON DELETE CASCADE,
    side SMALLINT NOT NULL CHECK (side IN (1, 2)),
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_id, side)
);
"""


class Database:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.database_pool_min_size,
            max_size=settings.database_pool_max_size,
            command_timeout=30,
        )
        async with self._pool.acquire() as conn:
            revision = None
            try:
                revision = await conn.fetchval(
                    "SELECT version_num FROM alembic_version"
                )
            except Exception:
                revision = None
            if revision != settings.expected_schema_revision:
                raise RuntimeError(
                    "PostgreSQL schema is not at the expected Alembic revision "
                    f"{settings.expected_schema_revision} (got {revision!r}). "
                    "Start Processing first so it can run migrations."
                )
        logger.info("[POSTGRES] Chunking connected schema=%s", revision)

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgreSQL is not started")
        return self._pool

    async def count_users(self) -> int:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM users"))

    async def bootstrap_admin(self) -> None:
        username = settings.bootstrap_admin_username.strip()
        password = settings.bootstrap_admin_password
        if not username or not password:
            logger.warning(
                "[AUTH] users table is empty and bootstrap admin password "
                "is not set; login will fail until an admin is created"
            )
            return
        if await self.count_users() > 0:
            return
        await self.create_user(
            username=username,
            password=password,
            role="admin",
            created_by=None,
        )
        logger.info("[AUTH] bootstrap admin created: %s", username)

    async def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str,
        created_by: uuid.UUID | None,
    ) -> dict[str, Any]:
        pool = self._require_pool()
        user_id = uuid.uuid4()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (
                    id, username, password_hash, role, created_by
                )
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, username, role, is_active, created_at
                """,
                user_id,
                username,
                hash_password(password),
                role,
                created_by,
            )
        return dict(row)

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, password_hash, role, is_active, created_at
                FROM users
                WHERE lower(username) = lower($1)
                """,
                username,
            )
        return None if row is None else dict(row)

    async def get_user_by_id(self, user_id: uuid.UUID) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, role, is_active, created_at
                FROM users
                WHERE id = $1
                """,
                user_id,
            )
        return None if row is None else dict(row)

    async def list_users(self) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, username, role, is_active, created_at
                FROM users
                ORDER BY created_at
                """
            )
        return [dict(row) for row in rows]

    async def update_user(
        self,
        user_id: uuid.UUID,
        *,
        is_active: bool | None = None,
        password: str | None = None,
    ) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM users WHERE id = $1",
                user_id,
            )
            if row is None:
                return None
            if is_active is not None:
                await conn.execute(
                    "UPDATE users SET is_active = $2 WHERE id = $1",
                    user_id,
                    is_active,
                )
            if password:
                await conn.execute(
                    "UPDATE users SET password_hash = $2 WHERE id = $1",
                    user_id,
                    hash_password(password),
                )
            updated = await conn.fetchrow(
                """
                SELECT id, username, role, is_active, created_at
                FROM users
                WHERE id = $1
                """,
                user_id,
            )
        return None if updated is None else dict(updated)

    async def set_password(self, user_id: uuid.UUID, password: str) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET password_hash = $2 WHERE id = $1",
                user_id,
                hash_password(password),
            )

    async def create_session(
        self,
        user_id: uuid.UUID,
        token: str,
    ) -> dict[str, Any]:
        pool = self._require_pool()
        session_id = uuid.uuid4()
        expires_at = session_expiry()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sessions (
                    id, token_hash, user_id, expires_at
                )
                VALUES ($1, $2, $3, $4)
                RETURNING id, user_id, expires_at
                """,
                session_id,
                hash_session_token(token),
                user_id,
                expires_at,
            )
        return dict(row)

    async def get_session_user(self, token: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        token_hash = hash_session_token(token)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    s.id AS session_id,
                    u.id,
                    u.username,
                    u.role,
                    u.is_active
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = $1
                  AND s.revoked_at IS NULL
                  AND s.expires_at > NOW()
                  AND u.is_active = TRUE
                """,
                token_hash,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE sessions
                SET last_seen_at = NOW(),
                    expires_at = GREATEST(expires_at, $2)
                WHERE id = $1
                """,
                row["session_id"],
                session_expiry(),
            )
        return dict(row)

    async def revoke_session(self, token: str) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE sessions
                SET revoked_at = NOW()
                WHERE token_hash = $1 AND revoked_at IS NULL
                """,
                hash_session_token(token),
            )

    async def revoke_user_sessions(
        self,
        user_id: uuid.UUID,
        *,
        except_token: str | None = None,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            if except_token:
                await conn.execute(
                    """
                    UPDATE sessions
                    SET revoked_at = NOW()
                    WHERE user_id = $1
                      AND revoked_at IS NULL
                      AND token_hash <> $2
                    """,
                    user_id,
                    hash_session_token(except_token),
                )
            else:
                await conn.execute(
                    """
                    UPDATE sessions
                    SET revoked_at = NOW()
                    WHERE user_id = $1 AND revoked_at IS NULL
                    """,
                    user_id,
                )

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    SELECT
                        j.*,
                        r.verdict
                    FROM comparison_jobs j
                    LEFT JOIN LATERAL (
                        SELECT verdict
                        FROM comparison_results
                        WHERE job_id = j.job_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) r ON TRUE
                    WHERE j.job_id = $1
                    """,
                    job_id,
                )
            except UndefinedTableError:
                row = await conn.fetchrow(
                    "SELECT * FROM comparison_jobs WHERE job_id = $1",
                    job_id,
                )
        return None if row is None else dict(row)

    async def list_jobs_for_user(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        pool = self._require_pool()
        query_with_verdict = """
            SELECT
                j.job_id,
                j.document_id,
                j.user_id,
                j.file1_name,
                j.file2_name,
                j.status,
                j.last_message,
                j.processed_chunks,
                j.total_chunks,
                j.created_at,
                j.updated_at,
                r.verdict,
                CASE
                    WHEN j.status IN (
                        'completed', 'failed', 'cancelled', 'deleted'
                    ) THEN NULL
                    ELSE (
                        SELECT COUNT(*)
                        FROM comparison_jobs q
                        WHERE q.user_id = j.user_id
                          AND q.status IN (
                              'queued', 'preparing', 'processing', 'ocr_ready',
                              'comparing', 'classifying', 'finalizing'
                          )
                          AND q.created_at <= j.created_at
                    )
                END AS queue_position,
                (
                    SELECT COUNT(*)
                    FROM comparison_jobs a
                    WHERE a.user_id = j.user_id
                      AND a.status IN (
                          'queued', 'preparing', 'processing', 'ocr_ready',
                          'comparing', 'classifying', 'finalizing'
                      )
                ) AS active_count
            FROM comparison_jobs j
            LEFT JOIN LATERAL (
                SELECT verdict
                FROM comparison_results
                WHERE job_id = j.job_id
                ORDER BY created_at DESC
                LIMIT 1
            ) r ON TRUE
            WHERE j.user_id = $1
              AND j.status <> 'deleted'
            ORDER BY j.created_at DESC
        """
        query_plain = """
            SELECT
                job_id,
                document_id,
                user_id,
                file1_name,
                file2_name,
                status,
                last_message,
                processed_chunks,
                total_chunks,
                created_at,
                updated_at
            FROM comparison_jobs
            WHERE user_id = $1
            ORDER BY created_at DESC
        """
        async with pool.acquire() as conn:
            try:
                rows = await conn.fetch(query_with_verdict, user_id)
            except UndefinedTableError:
                rows = await conn.fetch(query_plain, user_id)
        return [dict(row) for row in rows]

    async def insert_job_file(
        self,
        *,
        job_id: str,
        side: int,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        storage_path: str,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO job_files (
                    id,
                    job_id,
                    side,
                    original_filename,
                    content_type,
                    size_bytes,
                    sha256,
                    storage_path
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
                original_filename,
                content_type,
                size_bytes,
                sha256,
                storage_path,
            )

    async def get_job_file(
        self,
        job_id: str,
        side: int,
    ) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM job_files
                WHERE job_id = $1 AND side = $2
                """,
                job_id,
                side,
            )
        return None if row is None else dict(row)

    async def list_job_files(self, job_id: str) -> list[dict[str, Any]]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM job_files
                WHERE job_id = $1
                ORDER BY side
                """,
                job_id,
            )
        return [dict(row) for row in rows]

    async def delete_job(self, job_id: str) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM comparison_jobs WHERE job_id = $1",
                job_id,
            )
        return result == "DELETE 1"


    @property
    def pool(self) -> asyncpg.Pool:
        return self._require_pool()

    async def get_comparison_result(self, job_id: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT comparison_json
                FROM comparison_results
                WHERE job_id = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                job_id,
            )
        if value is None:
            return None
        if isinstance(value, str):
            import json

            return json.loads(value)
        return dict(value) if not isinstance(value, dict) else value

    async def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE comparison_jobs
                SET cancel_requested_at = COALESCE(cancel_requested_at, NOW()),
                    status = CASE
                        WHEN status IN ('completed', 'failed', 'cancelled', 'deleted', 'deleting')
                        THEN status
                        ELSE 'cancel_requested'
                    END,
                    last_message = CASE
                        WHEN status IN ('completed', 'failed', 'cancelled', 'deleted', 'deleting')
                        THEN last_message
                        ELSE 'Отмена запрошена'
                    END,
                    state_version = state_version + 1,
                    updated_at = NOW()
                WHERE job_id = $1
                RETURNING *
                """,
                job_id,
            )
        return None if row is None else dict(row)

    async def begin_delete(self, job_id: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE comparison_jobs
                SET status = 'deleting',
                    last_message = 'Удаление…',
                    state_version = state_version + 1,
                    updated_at = NOW()
                WHERE job_id = $1 AND status <> 'deleted'
                RETURNING *
                """,
                job_id,
            )
        return None if row is None else dict(row)

    async def finish_delete(self, job_id: str) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM comparison_jobs WHERE job_id = $1",
                job_id,
            )
        return result == "DELETE 1"


database = Database()
