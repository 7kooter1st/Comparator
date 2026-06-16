from flask import Blueprint, send_from_directory

from app.config import FRONTEND_DIST

frontend_bp = Blueprint("frontend", __name__)


@frontend_bp.route("/", defaults={"path": ""})
@frontend_bp.route("/<path:path>")
def serve_spa(path: str):
    """Раздаёт собранный Vite-фронтенд (SPA). API — под /api."""
    if not FRONTEND_DIST.is_dir():
        return (
            "Фронтенд не собран. Положите содержимое dist в "
            f"{FRONTEND_DIST} или задайте FRONTEND_DIST в .env",
            404,
        )

    if path.startswith("api"):
        return {"error": "Not found"}, 404

    requested = FRONTEND_DIST / path
    if path and requested.is_file():
        return send_from_directory(FRONTEND_DIST, path)

    return send_from_directory(FRONTEND_DIST, "index.html")
