from flask import Flask
from flask_cors import CORS

from app.config import FRONTEND_DIST
from app.routes.compare import compare_bp
from app.routes.frontend import frontend_bp


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)

    app.register_blueprint(compare_bp, url_prefix="/api")

    if FRONTEND_DIST.is_dir():
        app.register_blueprint(frontend_bp)

    return app
