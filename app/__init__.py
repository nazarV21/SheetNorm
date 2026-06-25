from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from werkzeug.exceptions import RequestEntityTooLarge

from config import Config
from .extensions import db as db_ext, login_manager, migrate
from .routes.api import api_bp
from .routes.web import web_bp
from .cli import register_cli


def create_app(config_class: type[Config] = Config) -> Flask:
    # Явно указываем папку с шаблонами (../templates от пакета app)
    app = Flask(__name__, template_folder="../templates")
    app.config.from_object(config_class)
    if hasattr(config_class, "validate") and not app.config.get("TESTING"):
        config_class.validate()

    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}})
    db_ext.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "web.dashboard"

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            from app.db.models import User

            return db_ext.session.get(User, user_id)
        except Exception:
            return None

    if migrate is not None:
        migrate.init_app(app, db_ext)
    register_cli(app)
    _auto_create_local_sqlite_db(app)

    register_blueprints(app)

    @app.get("/health")
    def healthcheck():
        return {"status": "ok", "service": "SheetNorm"}

    @app.get("/health/live")
    def health_live():
        return {"status": "ok", "service": "SheetNorm"}

    @app.get("/health/ready")
    def health_ready():
        payload = _health_payload(app)
        status_code = 200 if payload["status"] == "ok" else 503
        return payload, status_code

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.errorhandler(RequestEntityTooLarge)
    def handle_large_upload(_error):
        limit_mb = int(app.config["MAX_CONTENT_LENGTH"] / 1024 / 1024)
        if request.path.startswith("/api/"):
            return jsonify(
                {
                    "error": "Файл слишком большой",
                    "details": f"Размер запроса превышает лимит {limit_mb} МБ.",
                    "suggestion": "Уменьшите файл или измените MAX_UPLOAD_MB для контролируемого развёртывания.",
                    "code": "FILE_TOO_LARGE",
                }
            ), 413
        return render_template("error.html", title="Файл слишком большой", details=f"Лимит загрузки: {limit_mb} МБ.", suggestion="Уменьшите размер Excel-файла и повторите загрузку."), 413

    @app.errorhandler(500)
    def handle_internal_error(_error):
        if request.path.startswith("/api/"):
            return jsonify(
                {
                    "error": "Внутренняя ошибка обработки",
                    "details": "Запрос не удалось выполнить.",
                    "suggestion": "Повторите запрос и проверьте журнал приложения.",
                    "code": "INTERNAL_ERROR",
                }
            ), 500
        return render_template("error.html", title="Не удалось выполнить операцию", details="Приложение остановило обработку до сохранения некорректного результата.", suggestion="Проверьте файл и инструкцию. Если ошибка повторяется, изучите журнал приложения."), 500

    return app


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")


def _auto_create_local_sqlite_db(app: Flask) -> None:
    if app.config.get("DATA_STORE_BACKEND") != "database":
        return
    if not app.config.get("AUTO_CREATE_SQLITE_DB"):
        return

    database_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI", ""))
    if not database_uri.startswith("sqlite:"):
        return

    with app.app_context():
        import app.db.models  # noqa: F401

        db_ext.create_all()


def _health_payload(app: Flask) -> dict:
    from sqlalchemy import text

    checks = {
        "database": "skipped",
        "redis": "skipped",
        "storage": "ok",
        "queue": "sync" if app.config.get("ASYNC_MODE") != "rq" else "unknown",
    }
    try:
        with app.app_context():
            db_ext.session.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"

    storage_root = app.config.get("STORAGE_ROOT")
    try:
        storage_root.mkdir(parents=True, exist_ok=True)
        checks["storage"] = "ok"
    except Exception:
        checks["storage"] = "error"

    if app.config.get("ASYNC_MODE") == "rq":
        try:
            from app.workers.queue import get_redis_connection

            get_redis_connection().ping()
            checks["redis"] = "ok"
            checks["queue"] = "ok"
        except Exception:
            checks["redis"] = "error"
            checks["queue"] = "error"

    status = "ok" if all(value not in {"error"} for value in checks.values()) else "degraded"
    return {
        "status": status,
        "service": "SheetNorm",
        **checks,
        "ai_backend": app.config.get("AI_BACKEND", "fallback"),
        "script_execution": "enabled" if app.config.get("SCRIPT_EXECUTION_ENABLED") else "disabled",
    }

