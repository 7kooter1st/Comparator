from flask import Flask
from flask_cors import CORS

from app.routes.compare import compare_bp


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)

    app.register_blueprint(compare_bp, url_prefix="/api")

    return app
