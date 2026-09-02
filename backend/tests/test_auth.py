import unittest
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.models import AuthUser
from app.routers.auth import router as auth_router
from app.routers.jobs import require_owned_job, router as jobs_router
from app.security import hash_password, hash_session_token, verify_password


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self) -> None:
        hashed = hash_password("secret-pass")
        self.assertTrue(verify_password("secret-pass", hashed))
        self.assertFalse(verify_password("other", hashed))

    def test_session_token_hash_is_stable(self) -> None:
        self.assertEqual(
            hash_session_token("abc"),
            hash_session_token("abc"),
        )
        self.assertNotEqual(
            hash_session_token("abc"),
            hash_session_token("abd"),
        )


class OwnershipTests(unittest.TestCase):
    def test_missing_job_is_404(self) -> None:
        user = AuthUser(
            id=uuid4(),
            username="alice",
            role="user",
            is_active=True,
        )
        with self.assertRaises(HTTPException) as ctx:
            require_owned_job(None, user)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_foreign_job_is_403(self) -> None:
        owner = uuid4()
        other = AuthUser(
            id=uuid4(),
            username="bob",
            role="user",
            is_active=True,
        )
        with self.assertRaises(HTTPException) as ctx:
            require_owned_job({"job_id": "j1", "user_id": owner}, other)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_is_allowed(self) -> None:
        user_id = uuid4()
        user = AuthUser(
            id=user_id,
            username="alice",
            role="user",
            is_active=True,
        )
        job = {"job_id": "j1", "user_id": user_id}
        self.assertIs(require_owned_job(job, user), job)

    def test_admin_can_delete_foreign_job_only_when_allowed(self) -> None:
        admin = AuthUser(
            id=uuid4(),
            username="admin",
            role="admin",
            is_active=True,
        )
        job = {"job_id": "j1", "user_id": uuid4()}
        with self.assertRaises(HTTPException):
            require_owned_job(job, admin)
        self.assertIs(require_owned_job(job, admin, allow_admin=True), job)


class FakeDB:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.files: dict[tuple[str, int], dict] = {}
        self.sessions: list[str] = []

    async def get_user_by_username(self, username: str):
        return self.users.get(username.lower())

    async def create_session(self, user_id, token: str):
        self.sessions.append(token)
        return {"id": uuid4(), "user_id": user_id}

    async def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    async def list_jobs_for_user(self, user_id):
        return [
            job
            for job in self.jobs.values()
            if job["user_id"] == user_id
        ]

    async def get_job_file(self, job_id: str, side: int):
        return self.files.get((job_id, side))

    async def delete_job(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None


class AuthLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakeDB()
        user_id = uuid4()
        self.db.users["admin"] = {
            "id": user_id,
            "username": "admin",
            "password_hash": hash_password("secret12"),
            "role": "admin",
            "is_active": True,
        }
        app = FastAPI()
        app.include_router(auth_router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def test_login_rejects_bad_password(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrongpass"},
        )
        self.assertEqual(response.status_code, 401)

    def test_login_sets_http_only_cookie(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "secret12"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "admin")
        self.assertIn("comparator_session", response.cookies)
        cookie_header = response.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie_header)


class JobIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = AuthUser(
            id=uuid4(),
            username="alice",
            role="user",
            is_active=True,
        )
        self.intruder = AuthUser(
            id=uuid4(),
            username="bob",
            role="user",
            is_active=True,
        )
        self.db = FakeDB()
        self.db.jobs["job-1"] = {
            "job_id": "job-1",
            "user_id": self.owner.id,
            "file1_name": "a.pdf",
            "file2_name": "b.pdf",
            "status": "completed",
            "last_message": "done",
            "processed_chunks": 1,
            "total_chunks": 1,
            "verdict": "different",
            "created_at": None,
            "updated_at": None,
        }
        app = FastAPI()
        app.include_router(jobs_router)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.intruder
        self.client = TestClient(app)

    def test_foreign_job_status_is_403(self) -> None:
        response = self.client.get("/api/jobs/job-1")
        self.assertEqual(response.status_code, 403)

    def test_foreign_job_result_is_403(self) -> None:
        response = self.client.get("/api/jobs/job-1/result")
        self.assertEqual(response.status_code, 403)

    def test_foreign_job_download_is_403(self) -> None:
        response = self.client.get("/api/jobs/job-1/files/1")
        self.assertEqual(response.status_code, 403)

    def test_list_does_not_include_foreign_jobs(self) -> None:
        response = self.client.get("/api/jobs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
