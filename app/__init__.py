from flask import Flask
from flask_cors import CORS

from config import Config
from .routes.api import api_bp
from .routes.web import web_bp


def create_app(config_class: type[Config] = Config) -> Flask:
    # Явно указываем папку с шаблонами (../templates от пакета app)
    app = Flask(__name__, template_folder="../templates")
    app.config.from_object(config_class)

    CORS(app, resources={r"/api/*": {"origins": "*"}})

    register_blueprints(app)

    @app.get("/health")
    def healthcheck():
        return {"status": "ok"}

    return app


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

