from __future__ import annotations

import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from flask import send_file

from app.db.repositories.templates import TemplateRepository
from app.scripts_runtime.validator import validate_pandas_script
from app.services.ai.instruction_assistant import InstructionAssistant
from app.services.ai.hardware_info import HardwareInfoService
from app.services.ai.model_manager import ModelLoadError, get_model_manager
from app.services.ai.model_recommendation import ModelRecommendationService
from app.services.ai.model_registry import ModelRegistry
from app.services.ai.settings_service import AISettingsService
from app.services.conversion_service import ConversionService
from app.services.table_structure_analyzer import TableStructureAnalyzer
from app.services.workbook_preview import build_workbook_preview, prompt_tips_from_analysis
from app.services.rule_schema import normalize_declarative_rule
from app.utils.rules_store import RulesStore
from app.utils.training_examples_store import TrainingExamplesStore
from app.utils.jobs_repository import JobsRepository
from app.utils.batches_repository import BatchesRepository
from app.utils.uploads import is_excel_filename, sanitize_upload_name, save_excel_upload, validate_excel_file
from app.workers.queue import cancel_conversion, enqueue_assistant_analysis, enqueue_conversion


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
    job = cancel_conversion(job_id)
    if not job:
        return _api_error("Job not found", f"ProcessingJob {job_id} is missing.", "Check job_id.", "JOB_NOT_FOUND", 404)
    return jsonify(job), 200


@api_bp.post("/jobs/<job_id>/retry")
def retry_job(job_id: str):
    repository = JobsRepository()
    existing = repository.get(job_id)
    if not existing:
        return _api_error("Job not found", f"ProcessingJob {job_id} is missing.", "Check job_id.", "JOB_NOT_FOUND", 404)
    if existing.get("status") not in {"failed", "cancelled"}:
        return _api_error("Job cannot be retried", "Only failed or cancelled jobs can be retried.", "Wait for the active job or open a terminal job.", "JOB_NOT_RETRYABLE", 409)
    repository.update_status(
        job_id,
        "created",
        stage="created",
        progress=0,
        queue_job_id=None,
        error_code=None,
        error_message=None,
        error_details={},
        output_filename=None,
        output_path=None,
        finished_at=None,
    )
    job = enqueue_conversion(
        job_id,
        rule_id=existing.get("rule_id"),
        instruction=existing.get("original_instruction") if not existing.get("rule_id") else None,
        improve_instruction=bool(existing.get("original_instruction") and not existing.get("rule_id")),
    )
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
    if not JobsRepository().get(job_id):
        return _api_error("Задача не найдена", f"ProcessingJob {job_id} отсутствует.", "Сначала загрузите Excel-файл.", "JOB_NOT_FOUND", 404)
    job = enqueue_conversion(
        job_id,
        schema=payload.get("schema"),
        options=payload.get("options", {}),
    )
    return jsonify(job), 202


@api_bp.post("/convert-with-instruction/<job_id>")
def convert_with_instruction(job_id: str):
    payload = request.get_json(silent=True) or {}
    instruction = (payload.get("instruction") or "").strip()
    if not instruction:
        return _api_error("Инструкция не указана", "Поле instruction пустое.", "Опишите ожидаемое преобразование простыми словами.", "INSTRUCTION_REQUIRED", 400)
    if not JobsRepository().get(job_id):
        return _api_error("Задача не найдена", f"ProcessingJob {job_id} отсутствует.", "Сначала загрузите Excel-файл.", "JOB_NOT_FOUND", 404)
    job = enqueue_conversion(
        job_id,
        instruction=instruction,
        generated_rule=payload.get("generated_rule") or {},
        options=payload.get("options") or {},
        improve_instruction=bool(payload.get("improve_instruction")),
    )
    return jsonify(job), 202


@api_bp.get("/batches/<batch_id>")
def get_batch(batch_id: str):
    batch = BatchesRepository().get(batch_id)
    if not batch:
        return _api_error(
            "Пакет не найден",
            f"Batch {batch_id} отсутствует.",
            "Проверьте ссылку или запустите пакетную обработку повторно.",
            "BATCH_NOT_FOUND",
            404,
        )
    repository = JobsRepository()
    jobs = [repository.get(job_id) for job_id in batch.get("job_ids") or []]
    jobs = [job for job in jobs if job]
    statuses: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "created")
        statuses[status] = statuses.get(status, 0) + 1
    terminal = all(job.get("status") in {"success", "failed", "cancelled"} for job in jobs) if jobs else True
    return jsonify({**batch, "jobs": jobs, "statuses": statuses, "terminal": terminal}), 200


@api_bp.post("/assistant/file-preview")
def assistant_file_preview():
    incoming = request.files.get("file")
    job_id = (request.form.get("job_id") or "").strip()
    requested_sheet = (request.form.get("sheet_name") or "").strip() or None
    raw_prompt = (request.form.get("raw_prompt") or "").strip()
    repository = JobsRepository()

    if incoming and incoming.filename:
        if not _is_excel_filename(incoming.filename):
            return _api_error(
                "Неподдерживаемый формат",
                "Предпросмотр принимает только Excel-файлы.",
                "Загрузите файл .xlsx или .xls.",
                "UNSUPPORTED_FILE_TYPE",
                400,
            )
        job_id, original_filename, path = save_excel_upload(incoming, current_app.config["INPUT_DIR"])
        valid, validation_error = validate_excel_file(path)
        if not valid:
            path.unlink(missing_ok=True)
            (Path(current_app.config["INPUT_DIR"]) / "meta" / f"{job_id}.meta.json").unlink(missing_ok=True)
            return _api_error(
                "Некорректный Excel-файл",
                validation_error,
                "Выберите неповреждённый Excel-файл.",
                "INVALID_EXCEL_FILE",
                400,
            )
        repository.create(
            job_id,
            input_filename=original_filename,
            input_path=path,
            job_kind="assistant",
            selected_sheet=requested_sheet,
            original_instruction=raw_prompt or None,
            assistant_state={"raw_prompt": raw_prompt, "selected_sheet": requested_sheet, "current_step": 1},
        )
    elif job_id:
        job = repository.get(job_id)
        if not job or job.get("job_kind") != "assistant":
            return _api_error("Сессия не найдена", "Рабочая сессия AI-помощника отсутствует.", "Выберите файл повторно.", "ASSISTANT_SESSION_NOT_FOUND", 404)
        path = Path(str(job.get("input_path") or ""))
        original_filename = str(job.get("input_filename") or path.name)
        if not path.exists():
            return _api_error("Файл не найден", "Исходный файл рабочей сессии отсутствует.", "Выберите файл повторно.", "SOURCE_FILE_NOT_FOUND", 404)
    else:
        return _api_error(
            "Файл не выбран",
            "Поле file отсутствует и job_id не передан.",
            "Выберите Excel-файл — предпросмотр появится до запуска AI-анализа.",
            "FILE_MISSING",
            400,
        )

    try:
        workbook_preview = build_workbook_preview(path, max_rows=40, max_columns=18)
        workbook_preview["filename"] = original_filename
        analysis = TableStructureAnalyzer().analyze_excel(path, sheet_name=requested_sheet, max_rows=40)
    except Exception as exc:
        current_app.logger.exception("Assistant file preview failed for %s", original_filename)
        return _api_error(
            "Не удалось построить предпросмотр",
            str(exc),
            "Проверьте файл, затем повторите загрузку. Техническая причина записана в журнал приложения.",
            "PREVIEW_FAILED",
            422,
        )

    state = dict((repository.get(job_id) or {}).get("assistant_state") or {})
    state.update(
        {
            "raw_prompt": raw_prompt or state.get("raw_prompt", ""),
            "selected_sheet": requested_sheet,
            "source_workbook_preview": workbook_preview,
            "prompt_tips": prompt_tips_from_analysis(analysis),
            "analysis_summary": {
                "sheets": analysis.get("sheets") or [],
                "selected_sheet": analysis.get("selected_sheet"),
                "header_rows_human": analysis.get("header_rows_human") or [],
                "data_start_row_human": analysis.get("data_start_row_human"),
                "table_type": analysis.get("table_type"),
                "merged_ranges_count": analysis.get("merged_ranges_count", 0),
            },
            "current_step": 1,
        }
    )
    repository.update_context(
        job_id,
        job_kind="assistant",
        selected_sheet=requested_sheet,
        resume_step=1,
        original_instruction=state.get("raw_prompt") or None,
        assistant_state=state,
    )

    return jsonify(
        {
            "job_id": job_id,
            "filename": original_filename,
            "workbook_preview": workbook_preview,
            "prompt_tips": state["prompt_tips"],
            "analysis": state["analysis_summary"],
        }
    ), 200


@api_bp.post("/jobs/<job_id>/assistant-state")
def save_assistant_state(job_id: str):
    repository = JobsRepository()
    job = repository.get(job_id)
    if not job or job.get("job_kind") != "assistant":
        return _api_error("Сессия не найдена", "Рабочая сессия AI-помощника отсутствует.", "Выберите файл повторно.", "ASSISTANT_SESSION_NOT_FOUND", 404)
    payload = request.get_json(silent=True) or {}
    state = dict(job.get("assistant_state") or {})
    if "raw_prompt" in payload:
        state["raw_prompt"] = str(payload.get("raw_prompt") or "")[:20000]
    if "sheet_name" in payload:
        state["selected_sheet"] = str(payload.get("sheet_name") or "") or None
    if "current_step" in payload:
        try:
            state["current_step"] = max(1, min(8, int(payload.get("current_step") or 1)))
        except (TypeError, ValueError):
            pass
    updated = repository.update_context(
        job_id,
        selected_sheet=state.get("selected_sheet"),
        resume_step=int(state.get("current_step") or 1),
        original_instruction=state.get("raw_prompt") or None,
        assistant_state=state,
    )
    return jsonify({"job_id": job_id, "saved": True, "assistant_state": updated.get("assistant_state") or state}), 200


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


@api_bp.get("/settings/ai")
def get_ai_settings():
    settings = get_model_manager().ensure_auto_selection()
    return jsonify({"settings": settings.as_dict(), "status": get_model_manager().status()}), 200


@api_bp.get("/settings/ai/models")
def list_ai_models():
    registry = ModelRegistry(current_app.config["AI_MODELS_DIR"])
    hardware = HardwareInfoService().collect()
    recommender = ModelRecommendationService()
    settings = get_model_manager().ensure_auto_selection()
    items = []
    for model in registry.scan_models():
        payload = model.as_dict()
        payload["recommendation"] = recommender.evaluate(model, hardware).as_dict()
        payload["selected"] = model.relative_path == settings.selected_model_relative_path
        payload["active"] = model.relative_path == settings.active_model_relative_path
        items.append(payload)
    return jsonify({"items": items, "models_dir": str(registry.root), "hardware": hardware.as_dict()}), 200


@api_bp.post("/settings/ai/models/refresh")
def refresh_ai_models_api():
    items = ModelRegistry(current_app.config["AI_MODELS_DIR"]).refresh()
    return jsonify({"count": len(items)}), 200


@api_bp.post("/settings/ai/auto-select")
def auto_select_ai_model_api():
    try:
        AISettingsService().save({"selection_mode": "auto"})
        settings = get_model_manager().ensure_auto_selection(force=True)
        return jsonify({"settings": settings.as_dict(), "status": get_model_manager().status()}), 200
    except (ValueError, ModelLoadError) as exc:
        return _api_error(
            "Automatic model selection failed",
            str(exc),
            "Check the models directory and available RAM.",
            "AI_AUTO_SELECTION_FAILED",
            422,
        )


@api_bp.post("/settings/ai/test")
def test_ai_model_api():
    payload = request.get_json(silent=True) or {}
    registry = ModelRegistry(current_app.config["AI_MODELS_DIR"])
    model_id = str(payload.get("model_id") or "").strip()
    try:
        model = registry.get_model(model_id)
        hardware = HardwareInfoService().collect()
        profile = str(payload.get("performance_profile") or "balanced")
        service = AISettingsService()
        values = {
            "selected_model_relative_path": model.relative_path,
            "performance_profile": profile,
            "memory_mode": str(payload.get("memory_mode") or "economy"),
            "idle_unload_seconds": payload.get("idle_unload_seconds") or 300,
        }
        if profile == "custom":
            values.update({key: payload.get(key) for key in ("context_tokens", "max_completion_tokens", "n_threads", "n_batch", "n_gpu_layers", "temperature")})
        else:
            values.update(service.profile_values(profile, hardware.physical_cpu_count, bool(hardware.gpu_name)))
        service.save(values)
        result = get_model_manager().test_selected_model()
        return jsonify(result), 200
    except (KeyError, ValueError, ModelLoadError) as exc:
        return _api_error("Model test failed", str(exc), "Choose another model or a lighter profile.", "AI_MODEL_TEST_FAILED", 422)


@api_bp.post("/settings/ai/activate")
def activate_ai_model_api():
    try:
        settings = get_model_manager().activate_selected_model()
        return jsonify({"settings": settings.as_dict(), "status": get_model_manager().status()}), 200
    except (ValueError, ModelLoadError) as exc:
        return _api_error("Model activation failed", str(exc), "Test the selected model and verify available RAM.", "AI_MODEL_ACTIVATION_FAILED", 422)


@api_bp.post("/settings/ai/unload")
def unload_ai_model_api():
    get_model_manager().unload_model()
    return jsonify({"status": get_model_manager().status()}), 200


@api_bp.post("/settings/ai/fallback")
def activate_ai_fallback_api():
    settings = get_model_manager().set_fallback()
    return jsonify({"settings": settings.as_dict(), "status": get_model_manager().status()}), 200
