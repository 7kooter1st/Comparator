from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.jobs import router as jobs_router

__all__ = ["admin_router", "auth_router", "jobs_router"]
