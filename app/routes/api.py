from __future__ import annotations

import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.services.ai.instruction_assistant import InstructionAssistant
from app.services.conversion_service import ConversionService
from app.utils.rules_store import RulesStore
from app.utils.training_examples_store import TrainingExamplesStore
from app.utils.jobs_repository import JobsRepository
from app.utils.uploads import is_excel_filename, sanitize_upload_name, save_excel_upload


api_bp = Blueprint("api", __name__)


def _sanitize_filename(name: str) -> str:
    return sanitize_upload_name(name)


def _is_excel_filename(name: str) -> bool:
    return is_excel_filename(name)


def _api_error(error: str, details: str, suggestion: str, code: str, status: int):
    return jsonify(
        {
            "error": error,
            "details": details,
            "suggestion": suggestion,
            "code": code,
        }
    ), status


def _strip_request_only_report_config(generated_rule: dict) -> dict:
    rule = dict(generated_rule or {})
    rule.pop("excel_report", None)
    rule.pop("user_intent", None)
    return rule


@api_bp.get("/ping")
def ping():
    return {"message": "pong"}


@api_bp.get("/jobs/<job_id>")
def get_job(job_id: str):
    job = JobsRepository().get(job_id)
    if not job:
        return _api_error(
            "Задача не найдена",
            f"ProcessingJob {job_id} отсутствует.",
            "Проверьте job_id или загрузите файл повторно.",
            "JOB_NOT_FOUND",
            404,
        )
    return jsonify(job), 200


@api_bp.post("/uploads")
def upload_file():
    if "file" not in request.files:
        return _api_error("Файл не найден", "В запросе отсутствует поле file.", "Передайте Excel-файл как multipart/form-data в поле file.", "FILE_MISSING", 400)
    incoming = request.files["file"]
    if incoming.filename == "":
        return _api_error("Файл не выбран", "Поле file не содержит имени файла.", "Выберите файл .xlsx или .xls.", "EMPTY_FILENAME", 400)
    if not _is_excel_filename(incoming.filename):
        return _api_error("Неподдерживаемый формат", "Расширение файла не входит в список разрешённых.", "Загрузите файл .xlsx или .xls.", "UNSUPPORTED_FILE_TYPE", 400)

    file_id, original_filename, target_path = save_excel_upload(incoming, current_app.config["INPUT_DIR"])
    JobsRepository().create(file_id, input_filename=original_filename, input_path=target_path)
    return jsonify(
        {
            "job_id": file_id,
            "filename": incoming.filename,
            "stored_as": target_path.name,
            "size_bytes": target_path.stat().st_size,
            "status": "uploaded",
        }
    ), 201


@api_bp.post("/convert/<job_id>")
def convert(job_id: str):
    payload = request.get_json(silent=True) or {}
    schema = payload.get("schema")
    options = payload.get("options", {})
    service = ConversionService()
    result = service.convert(job_id=job_id, schema=schema, options=options)
    if "error" in result:
        status = 404 if result.get("code") in {None, "JOB_NOT_FOUND"} else 422
        return _api_error(result["error"], result.get("details") or "Задание или входной файл не найден.", result.get("suggestion") or "Проверьте job_id или загрузите файл повторно.", result.get("code") or "JOB_NOT_FOUND", status)
    return jsonify(result), 202


@api_bp.post("/convert-with-instruction/<job_id>")
def convert_with_instruction(job_id: str):
    payload = request.get_json(silent=True) or {}
    instruction = (payload.get("instruction") or "").strip()
    generated_rule = payload.get("generated_rule") or {}
    if not instruction:
        return _api_error("Инструкция не указана", "Поле instruction пустое.", "Опишите ожидаемое преобразование простыми словами.", "INSTRUCTION_REQUIRED", 400)
    result = ConversionService().convert_with_instruction(job_id, instruction, generated_rule=generated_rule)
    if "error" in result:
        status = 404 if result.get("code") is None else 422
        return _api_error(result["error"], result.get("details") or "Конвертация не выполнена.", result.get("suggestion") or "Проверьте job_id и уточните инструкцию.", result.get("code") or "CONVERSION_FAILED", status)
    return jsonify(result), 202


@api_bp.post("/assistant/analyze")
def analyze_with_assistant():
    incoming = request.files.get("file")
    if not incoming or incoming.filename == "":
        return _api_error("Файл не выбран", "Поле file отсутствует или пустое.", "Передайте Excel-файл в поле file.", "FILE_MISSING", 400)
    if not _is_excel_filename(incoming.filename):
        return _api_error("Неподдерживаемый формат", "AI-помощник принимает только Excel-файлы.", "Загрузите файл .xlsx или .xls.", "UNSUPPORTED_FILE_TYPE", 400)
    raw_prompt = request.form.get("raw_prompt", "")
    sheet_name = request.form.get("sheet_name") or None
    with tempfile.TemporaryDirectory(prefix="assistant_api_") as tmp:
        path = Path(tmp) / _sanitize_filename(incoming.filename)
        incoming.save(path)
        result = InstructionAssistant().prepare_instruction(path, raw_prompt, sheet_name=sheet_name)
        if result.get("engine") == "fallback":
            result.setdefault("warnings", []).append(
                {
                    "code": "LLM_UNAVAILABLE_FALLBACK_USED",
                    "message": "Локальная LLM недоступна или отключена. Использован детерминированный fallback.",
                }
            )
        similar_rules = RulesStore().find_similar_rules((result.get("analysis") or {}).get("fingerprint") or {})
        result["similar_rules"] = similar_rules
        return jsonify(result), 200


@api_bp.post("/rules")
def create_rule():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    prompt = (payload.get("prompt") or payload.get("ai_improved_instruction") or "").strip()
    if not name or not prompt:
        return _api_error("Шаблон не заполнен", "Обязательны поля name и prompt.", "Передайте название и инструкцию обработки.", "RULE_FIELDS_REQUIRED", 400)
    generated_rule = payload.get("generated_rule") or {}
    if not isinstance(generated_rule, dict):
        return _api_error("Невалидное JSON-правило", "generated_rule должен быть JSON-объектом.", "Исправьте структуру generated_rule.", "INVALID_RULE_JSON", 400)
    rule = RulesStore().add_rule(
        name=name,
        prompt=prompt,
        raw_prompt=payload.get("raw_user_prompt"),
        generated_rule=_strip_request_only_report_config(generated_rule),
        fingerprint=payload.get("fingerprint") or {},
        description=payload.get("description"),
        domain=payload.get("domain") or "universal",
        use_raw_data=payload.get("use_raw_data", True),
        sheet_name=payload.get("sheet_name"),
        category=payload.get("category"),
        table_type=payload.get("table_type"),
        tags=payload.get("tags") or [],
    )
    return jsonify(rule), 201


@api_bp.post("/training/batch")
def import_training_batch():
    archive = request.files.get("batch_zip")
    if not archive or archive.filename == "":
        return _api_error("Архив не выбран", "Поле batch_zip отсутствует или пустое.", "Передайте ZIP-архив с обучающими парами.", "ARCHIVE_MISSING", 400)
    if Path(archive.filename).suffix.lower() != ".zip":
        return _api_error("Неподдерживаемый архив", "Файл должен иметь расширение .zip.", "Загрузите ZIP-архив с обучающими парами.", "INVALID_ARCHIVE_TYPE", 400)
    rule_id = request.form.get("rule_id") or None
    prompt = request.form.get("prompt", "").strip() or None
    store = TrainingExamplesStore()
    with tempfile.TemporaryDirectory(prefix="training_api_zip_") as tmp:
        zip_path = Path(tmp) / _sanitize_filename(archive.filename)
        archive.save(zip_path)
        summary = store.import_from_zip(zip_path, rule_id=rule_id, prompt=prompt)
    status = 201 if summary.get("added", 0) > 0 else 200
    return jsonify(summary), status
