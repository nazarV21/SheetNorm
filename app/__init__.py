from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from werkzeug.exceptions import RequestEntityTooLarge

from config import Config
from .routes.api import api_bp
from .routes.web import web_bp


def create_app(config_class: type[Config] = Config) -> Flask:
    # Явно указываем папку с шаблонами (../templates от пакета app)
    app = Flask(__name__, template_folder="../templates")
    app.config.from_object(config_class)

    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}})

    register_blueprints(app)

    @app.get("/health")
    def healthcheck():
        return {"status": "ok", "service": "SheetNorm"}

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

