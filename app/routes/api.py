from __future__ import annotations

import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from flask import send_file

from app.db.repositories.templates import TemplateRepository
from app.scripts_runtime.validator import validate_pandas_script
from app.services.ai.instruction_assistant import InstructionAssistant
from app.services.conversion_service import ConversionService
from app.services.rule_schema import normalize_declarative_rule
from app.utils.rules_store import RulesStore
from app.utils.training_examples_store import TrainingExamplesStore
from app.utils.jobs_repository import JobsRepository
from app.utils.uploads import is_excel_filename, sanitize_upload_name, save_excel_upload, validate_excel_file
from app.workers.queue import enqueue_conversion


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
    rule = normalize_declarative_rule(generated_rule or {})
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


@api_bp.get("/jobs")
def list_jobs():
    status = request.args.get("status") or None
    jobs = JobsRepository().list(status=status)
    page = max(int(request.args.get("page", "1") or 1), 1)
    per_page = min(max(int(request.args.get("per_page", "50") or 50), 1), 200)
    start = (page - 1) * per_page
    items = jobs[start : start + per_page]
    return jsonify({"items": items, "page": page, "per_page": per_page, "total": len(jobs)}), 200


@api_bp.post("/jobs")
def create_job():
    payload = request.get_json(silent=True) or {}
    job_id = (payload.get("job_id") or "").strip()
    if not job_id:
        return _api_error("Job id is required", "POST /api/uploads before creating a processing job, or pass job_id.", "Upload an Excel file first.", "JOB_ID_REQUIRED", 400)
    if not JobsRepository().get(job_id):
        return _api_error("Job not found", f"ProcessingJob {job_id} is missing.", "Upload an Excel file first.", "JOB_NOT_FOUND", 404)
    job = enqueue_conversion(
        job_id,
        rule_id=payload.get("rule_id"),
        instruction=payload.get("instruction"),
        generated_rule=payload.get("generated_rule") or {},
    )
    return jsonify(job), 202


@api_bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = JobsRepository().update_status(job_id, "cancelled", progress=0)
    if not job:
        return _api_error("Job not found", f"ProcessingJob {job_id} is missing.", "Check job_id.", "JOB_NOT_FOUND", 404)
    return jsonify(job), 200


@api_bp.post("/jobs/<job_id>/retry")
def retry_job(job_id: str):
    existing = JobsRepository().get(job_id)
    if not existing:
        return _api_error("Job not found", f"ProcessingJob {job_id} is missing.", "Check job_id.", "JOB_NOT_FOUND", 404)
    job = enqueue_conversion(job_id, rule_id=existing.get("rule_id"))
    return jsonify(job), 202


@api_bp.get("/jobs/<job_id>/events")
def job_events(job_id: str):
    repository = JobsRepository()
    if not repository.get(job_id):
        return _api_error("Job not found", f"ProcessingJob {job_id} is missing.", "Check job_id.", "JOB_NOT_FOUND", 404)
    if getattr(repository, "_use_database", False):
        return jsonify({"items": repository._database().events(job_id)}), 200
    return jsonify({"items": []}), 200


@api_bp.get("/jobs/<job_id>/result")
def job_result(job_id: str):
    job = JobsRepository().get(job_id)
    if not job:
        return _api_error("Job not found", f"ProcessingJob {job_id} is missing.", "Check job_id.", "JOB_NOT_FOUND", 404)
    output_path = job.get("output_path")
    if job.get("status") != "success":
        return _api_error("Result is not ready", "The job has not completed successfully.", "Open job diagnostics and rerun after fixing errors.", "RESULT_NOT_READY", 409)
    if not output_path or not Path(output_path).exists():
        return _api_error("Result not found", "The result artifact is not available.", "Wait until the job succeeds or rerun it.", "RESULT_NOT_FOUND", 404)
    output_dir = Path(current_app.config["OUTPUT_DIR"]).resolve()
    resolved = Path(output_path).resolve()
    if resolved.parent != output_dir:
        return _api_error("Result not found", "The result artifact is not available.", "Wait until the job succeeds or rerun it.", "RESULT_NOT_FOUND", 404)
    return send_file(resolved, as_attachment=True, download_name=job.get("output_filename") or resolved.name)


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
    valid, validation_error = validate_excel_file(target_path)
    if not valid:
        target_path.unlink(missing_ok=True)
        (Path(current_app.config["INPUT_DIR"]) / "meta" / f"{file_id}.meta.json").unlink(missing_ok=True)
        return _api_error(
            "Некорректный Excel-файл",
            validation_error,
            "Загрузите неповреждённый .xlsx/.xls файл с хотя бы одним листом.",
            "INVALID_EXCEL_FILE",
            400,
        )
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


@api_bp.post("/assistant/generate")
def assistant_generate():
    payload = request.get_json(silent=True) or {}
    instruction = (payload.get("instruction") or "").strip()
    execution_mode = payload.get("execution_mode") or "declarative_rule"
    if not instruction:
        return _api_error("Instruction required", "Field instruction is empty.", "Describe the expected table transformation.", "INSTRUCTION_REQUIRED", 400)
    if execution_mode == "pandas_script":
        script = (
            "def transform(df):\n"
            "    result = df.copy()\n"
            "    result = result.dropna(how='all').dropna(axis=1, how='all')\n"
            "    return result\n"
        )
        report = validate_pandas_script(script, max_code_length=current_app.config.get("SCRIPT_MAX_CODE_LENGTH", 30000)).as_dict()
        return jsonify({"execution_mode": "pandas_script", "script_code": script, "validation_report": report}), 200
    return jsonify(
        {
            "execution_mode": "declarative_rule",
            "rule_json": {"table_type": "flat", "operations": ["drop_empty"]},
            "improved_instruction": instruction,
        }
    ), 200


@api_bp.post("/assistant/preview")
def assistant_preview():
    payload = request.get_json(silent=True) or {}
    job_id = (payload.get("job_id") or "").strip()
    instruction = (payload.get("instruction") or "").strip()
    if not job_id or not instruction:
        return _api_error("Preview fields required", "job_id and instruction are required.", "Upload a file and pass an instruction.", "PREVIEW_FIELDS_REQUIRED", 400)
    result = ConversionService().preview_with_instruction(
        job_id,
        instruction,
        generated_rule=payload.get("generated_rule") or {},
        max_rows=int(payload.get("max_rows") or 100),
    )
    status = 422 if "error" in result else 200
    serializable = {key: value for key, value in result.items() if key != "dataframe"}
    return jsonify(serializable), status


@api_bp.get("/templates")
def list_templates():
    try:
        items = TemplateRepository().list_templates(
            workspace_id=request.args.get("workspace_id") or None,
            limit=min(int(request.args.get("per_page", 50)), 200),
            offset=(max(int(request.args.get("page", 1)), 1) - 1) * min(int(request.args.get("per_page", 50)), 200),
        )
    except Exception as exc:
        return _api_error("Templates unavailable", str(exc), "Run database migrations and retry.", "DATABASE_UNAVAILABLE", 503)
    return jsonify({"items": items}), 200


@api_bp.post("/templates")
def create_template():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return _api_error("Template name required", "Field name is empty.", "Pass a template name.", "TEMPLATE_NAME_REQUIRED", 400)
    try:
        template = TemplateRepository().create_template(
            workspace_id=payload.get("workspace_id"),
            name=name,
            description=payload.get("description") or "",
            execution_mode=payload.get("execution_mode") or "declarative_rule",
            created_by_id=payload.get("created_by_id"),
        )
    except Exception as exc:
        return _api_error("Template not created", str(exc), "Check database state and payload.", "TEMPLATE_CREATE_FAILED", 422)
    return jsonify(template), 201


@api_bp.get("/templates/<template_id>")
def get_template(template_id: str):
    template = TemplateRepository().get_template(template_id)
    if not template:
        return _api_error("Template not found", f"Template {template_id} is missing.", "Check template id.", "TEMPLATE_NOT_FOUND", 404)
    return jsonify(template), 200


@api_bp.post("/templates/<template_id>/versions")
def create_template_version(template_id: str):
    payload = request.get_json(silent=True) or {}
    mode = payload.get("execution_mode") or "declarative_rule"
    report = {"valid": True, "errors": [], "warnings": []}
    if mode == "pandas_script":
        report = validate_pandas_script(
            payload.get("script_code") or "",
            max_code_length=current_app.config.get("SCRIPT_MAX_CODE_LENGTH", 30000),
        ).as_dict()
    try:
        version = TemplateRepository().add_version(
            template_id,
            source_instruction=payload.get("source_instruction") or "",
            improved_instruction=payload.get("improved_instruction") or "",
            execution_mode=mode,
            rule_json=payload.get("rule_json"),
            script_code=payload.get("script_code"),
            script_explanation=payload.get("script_explanation"),
            created_by_id=payload.get("created_by_id"),
            change_summary=payload.get("change_summary") or "",
        )
        if version is None:
            return _api_error("Template not found", f"Template {template_id} is missing.", "Create the template first.", "TEMPLATE_NOT_FOUND", 404)
        TemplateRepository().validate_version(version["id"], report)
        version["validation_report_json"] = report
        version["validation_status"] = "valid" if report.get("valid") else "invalid"
    except Exception as exc:
        return _api_error("Template version not created", str(exc), "Check database state and payload.", "VERSION_CREATE_FAILED", 422)
    return jsonify(version), 201


@api_bp.post("/template-versions/<version_id>/validate")
def validate_template_version(version_id: str):
    payload = request.get_json(silent=True) or {}
    if "script_code" in payload:
        report = validate_pandas_script(
            payload.get("script_code") or "",
            max_code_length=current_app.config.get("SCRIPT_MAX_CODE_LENGTH", 30000),
        ).as_dict()
    else:
        report = {"valid": True, "errors": [], "warnings": []}
    version = TemplateRepository().validate_version(version_id, report)
    if not version:
        return _api_error("Version not found", f"TemplateVersion {version_id} is missing.", "Check version id.", "VERSION_NOT_FOUND", 404)
    return jsonify(version), 200


@api_bp.post("/template-versions/<version_id>/approve")
def approve_template_version(version_id: str):
    try:
        version = TemplateRepository().approve_version(version_id, approved_by_id=(request.get_json(silent=True) or {}).get("approved_by_id"))
    except ValueError as exc:
        return _api_error("Version cannot be approved", str(exc), "Validate the version first.", "VERSION_NOT_VALID", 409)
    if not version:
        return _api_error("Version not found", f"TemplateVersion {version_id} is missing.", "Check version id.", "VERSION_NOT_FOUND", 404)
    return jsonify(version), 200


@api_bp.post("/template-versions/<version_id>/reject")
def reject_template_version(version_id: str):
    version = TemplateRepository().reject_version(version_id, report=request.get_json(silent=True) or {})
    if not version:
        return _api_error("Version not found", f"TemplateVersion {version_id} is missing.", "Check version id.", "VERSION_NOT_FOUND", 404)
    return jsonify(version), 200


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
