from __future__ import annotations

import json
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from flask import (
    Blueprint,
    current_app,
    jsonify,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

from app.services.ai.instruction_assistant import InstructionAssistant
from app.services.ai.hardware_info import HardwareInfoService
from app.services.ai.model_manager import ModelLoadError, get_model_manager
from app.services.ai.model_recommendation import ModelRecommendationService
from app.services.ai.model_registry import ModelRegistry
from app.services.ai.settings_service import AISettingsService
from app.services.conversion_service import ConversionService
from app.services.table_structure_analyzer import TableStructureAnalyzer
from app.services.workbook_preview import build_workbook_preview
from app.services.rule_schema import normalize_declarative_rule
from app.utils.rules_store import RulesStore
from app.utils.training_examples_store import TrainingExamplesStore
from app.utils.feedback_store import FeedbackStore
from app.utils.jobs_repository import JobsRepository
from app.utils.batches_repository import BatchesRepository
from app.utils.uploads import clone_excel_input, is_excel_filename, sanitize_upload_name, save_excel_upload, validate_excel_file
from app.workers.queue import cancel_conversion, enqueue_assistant_analysis, enqueue_conversion


web_bp = Blueprint("web", __name__)
def _sanitize_filename(name: str) -> str:
    return sanitize_upload_name(name)


def _is_excel_filename(name: str) -> bool:
    return is_excel_filename(name)


def _save_uploaded_file(file) -> tuple[str, str, Path]:
    job_id, original_filename, target_path = save_excel_upload(file, current_app.config["INPUT_DIR"])
    valid, validation_error = validate_excel_file(target_path)
    if not valid:
        target_path.unlink(missing_ok=True)
        (Path(current_app.config["INPUT_DIR"]) / "meta" / f"{job_id}.meta.json").unlink(missing_ok=True)
        raise ValueError(validation_error)
    JobsRepository().create(
        job_id,
        input_filename=original_filename,
        input_path=target_path,
    )
    return job_id, original_filename, target_path


def _parse_json_field(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _parse_rule_json(value: str | None) -> tuple[dict[str, Any], list[str]]:
    if not value or not value.strip():
        return {}, []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return {}, [f"JSON-правило не разобрано: строка {exc.lineno}, столбец {exc.colno}."]
    if not isinstance(parsed, dict):
        return {}, ["JSON-правило должно быть объектом, а не массивом или строкой."]
    return parsed, []


def _strip_request_only_report_config(generated_rule: dict[str, Any]) -> dict[str, Any]:
    rule = normalize_declarative_rule(generated_rule or {})
    rule.pop("excel_report", None)
    rule.pop("user_intent", None)
    return rule


@web_bp.get("/")
def dashboard():
    # SheetNorm is a converter first: the root URL renders the conversion workspace.
    return index()


@web_bp.post("/")
def legacy_index_post():
    return index()


@web_bp.route("/convert", methods=["GET", "POST"])
def index():
    rules_store = RulesStore()
    rules = rules_store.list_rules()

    if request.method == "POST":
        uploaded_files = []
        for field_name in ("files", "folder_files", "file"):
            uploaded_files.extend(
                file for file in request.files.getlist(field_name)
                if file and getattr(file, "filename", "")
            )

        excel_files = [file for file in uploaded_files if _is_excel_filename(file.filename)]
        skipped_names = [Path(file.filename).name for file in uploaded_files if not _is_excel_filename(file.filename)]
        if not excel_files:
            message = "Выберите один или несколько Excel-файлов .xlsx/.xls."
            if request.headers.get("X-SheetNorm-Folder-Mode") == "1":
                return jsonify({"error": message, "code": "FILES_REQUIRED"}), 400
            flash(message, "error")
            return redirect(url_for("web.index"))

        mode = request.form.get("mode", "rule")
        rule_id = request.form.get("rule_id") or None
        custom_instruction = request.form.get("custom_instruction", "").strip()
        should_improve = request.form.get("improve_instruction") == "1"
        save_as_rule = request.form.get("save_as_rule") == "1"
        new_rule_name = request.form.get("new_rule_name", "").strip()
        source_mode = request.form.get("source_mode", "files").strip() or "files"
        folder_name = request.form.get("folder_name", "").strip()

        if mode == "custom" and not custom_instruction:
            message = "Опишите ожидаемое преобразование."
            if request.headers.get("X-SheetNorm-Folder-Mode") == "1":
                return jsonify({"error": message, "code": "INSTRUCTION_REQUIRED"}), 400
            flash(message, "error")
            return redirect(url_for("web.index"))

        job_ids: list[str] = []
        filenames: list[str] = []
        errors: list[str] = []

        for uploaded in excel_files:
            try:
                job_id, original_filename, _target_path = _save_uploaded_file(uploaded)
            except ValueError as exc:
                errors.append(f"{Path(uploaded.filename).name}: {exc}")
                continue

            if mode == "custom":
                rule_name = new_rule_name
                if save_as_rule and not rule_name:
                    rule_name = f"Шаблон для {Path(original_filename).stem}"
                queued_job = enqueue_conversion(
                    job_id,
                    instruction=custom_instruction,
                    improve_instruction=should_improve,
                    save_as_rule=save_as_rule,
                    new_rule_name=rule_name,
                    domain=request.form.get("domain") or "universal",
                )
            else:
                queued_job = enqueue_conversion(job_id, rule_id=rule_id, options={})

            if queued_job.get("status") == "failed":
                errors.append(f"{original_filename}: {queued_job.get('error') or 'не удалось запустить обработку'}")
                continue
            job_ids.append(job_id)
            filenames.append(original_filename)

        if not job_ids:
            message = "; ".join(errors) or "Не удалось загрузить файлы."
            if request.headers.get("X-SheetNorm-Folder-Mode") == "1":
                return jsonify({"error": message, "code": "BATCH_UPLOAD_FAILED"}), 400
            flash(message, "error")
            return redirect(url_for("web.index"))

        if skipped_names:
            errors.append(f"Пропущены не-Excel файлы: {', '.join(skipped_names[:8])}")

        if len(job_ids) == 1 and source_mode == "files":
            if errors:
                flash("; ".join(errors), "warning")
            flash(
                "Задача запущена в фоне. Можно перейти на другую страницу — обработка продолжится. "
                "Остановить её можно в разделе «Задачи».",
                "success",
            )
            return redirect(url_for("web.job_detail", job_id=job_ids[0]))

        batch = BatchesRepository().create(
            job_ids=job_ids,
            filenames=filenames,
            source_mode=source_mode,
            folder_name=folder_name,
        )
        batch_url = url_for("web.batch_detail", batch_id=batch["batch_id"])
        if request.headers.get("X-SheetNorm-Folder-Mode") == "1":
            return jsonify(
                {
                    "batch_id": batch["batch_id"],
                    "url": batch_url,
                    "job_ids": job_ids,
                    "warnings": errors,
                }
            ), 202

        if errors:
            flash("; ".join(errors), "warning")
        flash(
            f"Запущено файлов: {len(job_ids)}. Обработка идёт в фоне; результаты доступны на странице пакета.",
            "success",
        )
        return redirect(batch_url)

    return render_template("index.html", rules=rules, selected_rule_id=request.args.get("rule_id", ""))


def _hydrate_batch(batch: dict[str, Any]) -> dict[str, Any]:
    repository = JobsRepository()
    jobs = [repository.get(job_id) for job_id in batch.get("job_ids") or []]
    jobs = [job for job in jobs if job]
    statuses = {status: 0 for status in ("created", "queued", "processing", "success", "failed", "cancelled")}
    for job in jobs:
        status = str(job.get("status") or "created")
        statuses[status] = statuses.get(status, 0) + 1
    terminal = all(job.get("status") in {"success", "failed", "cancelled"} for job in jobs) if jobs else True
    return {**batch, "jobs": jobs, "statuses": statuses, "terminal": terminal}


@web_bp.get("/batches/<batch_id>")
def batch_detail(batch_id: str):
    batch = BatchesRepository().get(batch_id)
    if not batch:
        return render_template(
            "error.html",
            title="Пакет не найден",
            details="Группа файлов отсутствует или была удалена.",
            suggestion="Вернитесь к конвертации и загрузите файлы повторно.",
        ), 404
    return render_template("batch_detail.html", batch=_hydrate_batch(batch))


@web_bp.post("/batches/<batch_id>/cancel")
def cancel_batch(batch_id: str):
    batch = BatchesRepository().get(batch_id)
    if not batch:
        flash("Пакет не найден.", "error")
        return redirect(url_for("web.jobs_list"))
    stopped = 0
    for job_id in batch.get("job_ids") or []:
        job = cancel_conversion(job_id)
        if job and job.get("status") == "cancelled":
            stopped += 1
    flash(f"Остановлено задач: {stopped}.", "info")
    return redirect(url_for("web.batch_detail", batch_id=batch_id))


@web_bp.get("/batches/<batch_id>/download")
def download_batch(batch_id: str):
    batch = BatchesRepository().get(batch_id)
    if not batch:
        return render_template(
            "error.html",
            title="Пакет не найден",
            details="Группа файлов отсутствует или была удалена.",
            suggestion="Вернитесь к конвертации и загрузите файлы повторно.",
        ), 404

    output_dir = Path(current_app.config["OUTPUT_DIR"]).resolve()
    archive_buffer = BytesIO()
    added = 0
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for job_id in batch.get("job_ids") or []:
            job = JobsRepository().get(job_id)
            if not job or job.get("status") != "success" or not job.get("output_path"):
                continue
            result_path = Path(str(job["output_path"])).resolve()
            if result_path.parent != output_dir or not result_path.exists():
                continue
            download_name = Path(str(job.get("output_filename") or result_path.name)).name
            archive.write(result_path, arcname=download_name)
            added += 1

    if not added:
        flash("В пакете пока нет готовых результатов для скачивания.", "warning")
        return redirect(url_for("web.batch_detail", batch_id=batch_id))

    archive_buffer.seek(0)
    folder_label = Path(str(batch.get("folder_name") or "sheetnorm_results")).name or "sheetnorm_results"
    return send_file(
        archive_buffer,
        as_attachment=True,
        download_name=f"{folder_label}_SheetNorm_results.zip",
        mimetype="application/zip",
    )


@web_bp.get("/about")
def about():
    return redirect(url_for("web.index"), code=302)


@web_bp.get("/deployment")
def deployment():
    return redirect(url_for("web.index"), code=302)


@web_bp.get("/jobs")
def jobs_list():
    selected_status = request.args.get("status", "").strip()
    query = request.args.get("q", "").strip().lower()
    jobs = JobsRepository().list(status=selected_status or None)
    for job in jobs:
        input_path = Path(str(job.get("input_path") or ""))
        output_path = Path(str(job.get("output_path") or ""))
        job["input_available"] = bool(job.get("input_path") and input_path.exists() and input_path.is_file())
        job["output_available"] = bool(
            job.get("status") == "success"
            and job.get("output_path")
            and output_path.exists()
            and output_path.is_file()
        )
    if query:
        jobs = [
            job for job in jobs
            if query in str(job.get("input_filename") or "").lower()
            or query in str(job.get("rule_name") or "").lower()
            or query in str(job.get("job_id") or "").lower()
            or query in str(job.get("original_instruction") or "").lower()
            or query in str(job.get("improved_instruction") or "").lower()
        ]
    return render_template(
        "jobs.html",
        jobs=jobs,
        selected_status=selected_status,
        query=request.args.get("q", ""),
    )


@web_bp.get("/jobs/<job_id>")
def job_detail(job_id: str):
    repository = JobsRepository()
    job = repository.get(job_id)
    if not job:
        return render_template(
            "error.html",
            title="Задача не найдена",
            details=f"Задача {job_id} отсутствует или была удалена.",
            suggestion="Вернитесь к списку задач или загрузите файл повторно.",
        ), 404

    result_workbook_preview = None
    draft_workbook_preview = None
    source_workbook_preview = None
    result_preview_error = None
    source_preview_error = None
    output_filename = None
    output_available = False
    input_available = False

    input_path_value = job.get("input_path")
    if input_path_value:
        input_path = Path(str(input_path_value)).resolve()
        if input_path.exists() and input_path.is_file():
            input_available = True
            try:
                source_workbook_preview = build_workbook_preview(input_path, max_rows=40, max_columns=18)
                source_workbook_preview["filename"] = job.get("input_filename") or input_path.name
            except Exception as exc:
                source_preview_error = f"Предпросмотр исходного файла недоступен: {exc}"
        else:
            source_preview_error = "Исходный файл больше не найден на диске. История задачи и инструкция сохранены, но повторная обработка требует исходного файла."

    output_path_value = job.get("output_path")
    if job.get("status") == "success" and output_path_value:
        output_dir = Path(current_app.config["OUTPUT_DIR"]).resolve()
        output_path = Path(str(output_path_value)).resolve()
        if output_path.parent == output_dir and output_path.exists() and output_path.is_file():
            output_available = True
            output_filename = output_path.name
            try:
                result_workbook_preview = build_workbook_preview(output_path, max_rows=60, max_columns=18)
                result_workbook_preview["filename"] = output_path.name
            except Exception as exc:
                result_preview_error = f"Предпросмотр результата недоступен: {exc}"
        else:
            result_preview_error = "Запись о результате сохранилась, но сам файл больше не найден в каталоге output."

    events = repository._database().events(job_id) if getattr(repository, "_use_database", False) else []
    assistant_state = dict(job.get("assistant_state") or {})
    if source_workbook_preview is None and assistant_state.get("source_workbook_preview"):
        source_workbook_preview = assistant_state.get("source_workbook_preview")
    if job.get("status") == "requires_review" and assistant_state.get("target_preview"):
        draft_workbook_preview = assistant_state.get("target_preview")

    return render_template(
        "job_detail.html",
        job=job,
        quality_report=job.get("quality_report") or {},
        result_workbook_preview=result_workbook_preview,
        draft_workbook_preview=draft_workbook_preview,
        source_workbook_preview=source_workbook_preview,
        result_preview_error=result_preview_error,
        source_preview_error=source_preview_error,
        output_filename=output_filename,
        output_available=output_available,
        input_available=input_available,
        events=events,
        assistant_state=assistant_state,
    )


@web_bp.post("/jobs/<job_id>/edit")
def edit_job(job_id: str):
    repository = JobsRepository()
    job = repository.get(job_id)
    if not job:
        flash("Задача не найдена.", "error")
        return redirect(url_for("web.jobs_list"))

    input_path_value = job.get("input_path")
    if not input_path_value:
        flash("У задачи не сохранён путь к исходному файлу.", "error")
        return redirect(url_for("web.job_detail", job_id=job_id))

    try:
        new_job_id, original_filename, new_input_path = clone_excel_input(
            input_path_value,
            job.get("input_filename") or Path(str(input_path_value)).name,
            current_app.config["INPUT_DIR"],
        )
        old_state = dict(job.get("assistant_state") or {})
        prompt = (job.get("original_instruction") or job.get("improved_instruction") or "").strip()
        source_preview = old_state.get("source_workbook_preview")
        if source_preview is None:
            try:
                source_preview = build_workbook_preview(new_input_path, max_rows=40, max_columns=18)
                source_preview["filename"] = original_filename
            except Exception:
                source_preview = None
        state = {
            "raw_prompt": prompt,
            "selected_sheet": job.get("selected_sheet"),
            "source_workbook_preview": source_preview,
            "current_step": 1,
            "cloned_from_job_id": job_id,
        }
        repository.create(
            new_job_id,
            input_filename=original_filename,
            input_path=new_input_path,
            original_instruction=prompt or None,
            job_kind="assistant",
            selected_sheet=job.get("selected_sheet"),
            execution_mode=job.get("execution_mode"),
            assistant_state=state,
        )
        flash("Создан новый черновик на основе прошлой задачи. Исторический результат сохранён без изменений.", "success")
        return redirect(url_for("web.instruction_assistant", job_id=new_job_id))
    except (FileNotFoundError, ValueError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = cancel_conversion(job_id)
    if not job:
        flash("Задача не найдена.", "error")
        return redirect(url_for("web.jobs_list"))
    if job.get("status") == "cancelled":
        flash("Задача остановлена. Уже завершённая операция pandas может закончить текущий шаг, но результат не будет сохранён.", "info")
    else:
        flash("Задача уже завершена, поэтому остановка не требуется.", "info")
    return redirect(request.referrer or url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/retry")
def retry_job(job_id: str):
    existing = JobsRepository().get(job_id)
    if not existing:
        flash("Задача не найдена.", "error")
        return redirect(url_for("web.jobs_list"))
    if existing.get("status") not in {"failed", "cancelled"}:
        flash("Повторный запуск доступен только для остановленных или завершившихся ошибкой задач.", "error")
        return redirect(url_for("web.job_detail", job_id=job_id))
    JobsRepository().update_status(
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
    if existing.get("job_kind") == "assistant":
        state = dict(existing.get("assistant_state") or {})
        enqueue_assistant_analysis(
            job_id,
            raw_prompt=state.get("raw_prompt") or existing.get("original_instruction") or "",
            sheet_name=state.get("selected_sheet") or existing.get("selected_sheet"),
            previous_ai_instruction=state.get("previous_ai_instruction") or existing.get("improved_instruction"),
            revision_number=int(state.get("revision_number") or 1),
        )
    else:
        enqueue_conversion(
            job_id,
            rule_id=existing.get("rule_id"),
            instruction=existing.get("original_instruction") if not existing.get("rule_id") else None,
            improve_instruction=bool(existing.get("original_instruction") and not existing.get("rule_id")),
        )
    flash("Задача поставлена в очередь повторно.", "success")
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.get("/download/<path:filename>")
def download(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename:
        return render_template(
            "error.html",
            title="Файл не найден",
            details="Запрошенный результат недоступен.",
            suggestion="Откройте карточку успешно завершённой задачи и скачайте файл оттуда.",
        ), 404
    output_dir = Path(current_app.config["OUTPUT_DIR"]).resolve()
    target = (output_dir / safe_name).resolve()
    if target.parent != output_dir or not target.exists():
        return render_template(
            "error.html",
            title="Файл не найден",
            details="Запрошенный результат недоступен.",
            suggestion="Откройте карточку успешно завершённой задачи и скачайте файл оттуда.",
        ), 404
    matching_job = next(
        (
            job for job in JobsRepository().list()
            if job.get("status") == "success" and Path(str(job.get("output_filename") or "")).name == safe_name
        ),
        None,
    )
    if not matching_job:
        return render_template(
            "error.html",
            title="Файл не найден",
            details="Для файла нет успешно завершённой задачи.",
            suggestion="Повторите обработку и скачайте результат из карточки задачи.",
        ), 404
    return send_from_directory(output_dir, safe_name, as_attachment=True)


@web_bp.route("/history", methods=["GET"])
def history():
    return redirect(url_for("web.jobs_list"), code=302)


@web_bp.route("/history/clear", methods=["POST"])
def clear_history():
    flash("История теперь объединена с разделом «Задачи».", "info")
    return redirect(url_for("web.jobs_list"))


def _ai_settings_context() -> dict[str, Any]:
    manager = get_model_manager()
    settings = manager.ensure_auto_selection()
    registry = ModelRegistry(current_app.config["AI_MODELS_DIR"])
    hardware = HardwareInfoService().collect()
    recommendation_service = ModelRecommendationService()
    models = []
    for model in registry.scan_models():
        item = model.as_dict()
        item["recommendation"] = recommendation_service.evaluate(model, hardware).as_dict()
        item["selected"] = model.relative_path == settings.selected_model_relative_path
        item["active"] = model.relative_path == settings.active_model_relative_path
        models.append(item)
    local_manager = current_app.extensions.get("sheetnorm_local_tasks")
    worker_status = local_manager.status() if local_manager is not None else {
        "mode": str(current_app.config.get("ASYNC_MODE", "thread")),
        "process_id": __import__("os").getpid(),
        "max_workers": int(current_app.config.get("LOCAL_WORKER_THREADS", 1)),
        "active_jobs": [],
        "active_count": 0,
    }
    return {
        "ai_settings": settings,
        "ai_models": models,
        "hardware": hardware,
        "model_manager_status": manager.status(),
        "worker_status": worker_status,
        "models_dir": registry.root,
    }


def _save_ai_settings_from_form() -> tuple[Any, dict[str, Any]]:
    registry = ModelRegistry(current_app.config["AI_MODELS_DIR"])
    hardware = HardwareInfoService().collect()
    model_id = (request.form.get("model_id") or "").strip()
    selected_path = None
    if model_id:
        selected_path = registry.get_model(model_id).relative_path
    profile = (request.form.get("performance_profile") or "balanced").strip()
    service = AISettingsService()
    selection_mode = (request.form.get("selection_mode") or "auto").strip()
    values: dict[str, Any] = {
        "selection_mode": selection_mode,
        "selected_model_relative_path": selected_path if selection_mode == "manual" else None,
        "performance_profile": profile,
        "auto_activate": request.form.get("auto_activate") == "on",
        "auto_test": request.form.get("auto_test") == "on",
        "reselect_if_unavailable": request.form.get("reselect_if_unavailable") == "on",
        "min_free_ram_gb": request.form.get("min_free_ram_gb") or 2,
        "max_ram_usage_ratio": request.form.get("max_ram_usage_ratio") or 0.72,
        "memory_mode": (request.form.get("memory_mode") or "economy").strip(),
        "idle_unload_seconds": request.form.get("idle_unload_seconds") or 300,
    }
    if profile == "custom":
        for key in ("context_tokens", "max_completion_tokens", "n_threads", "n_batch", "n_gpu_layers", "temperature"):
            values[key] = request.form.get(key)
    else:
        values.update(
            service.profile_values(
                profile,
                hardware.physical_cpu_count,
                bool(hardware.gpu_name),
            )
        )
    return service.save(values), values


@web_bp.get("/settings")
def settings():
    examples_store = TrainingExamplesStore()
    return render_template(
        "settings.html",
        config=current_app.config,
        training_stats=examples_store.get_stats(),
        **_ai_settings_context(),
    )


@web_bp.post("/settings/ai/save")
def save_ai_settings():
    try:
        settings_value, _ = _save_ai_settings_from_form()
        manager = get_model_manager()
        if settings_value.selection_mode == "auto":
            settings_value = manager.ensure_auto_selection(force=True)
            selected = settings_value.active_model_relative_path or "fallback"
            flash(f"Настройки сохранены. Автоматически назначено: {selected}.", "success")
        else:
            manager.unload_model()
            selected = settings_value.selected_model_relative_path or "не выбрана"
            flash(f"Настройки сохранены. Выбранная модель: {selected}.", "success")
    except (KeyError, ValueError) as exc:
        flash(f"Не удалось сохранить настройки модели: {exc}", "error")
    return redirect(url_for("web.settings"))


@web_bp.post("/settings/ai/test")
def test_ai_model():
    try:
        _save_ai_settings_from_form()
        result = get_model_manager().test_selected_model()
        flash(
            f"Модель успешно протестирована: загрузка {result['load_time_ms']} мс, "
            f"ответ {result['response_time_ms']} мс.",
            "success",
        )
    except (KeyError, ValueError, ModelLoadError) as exc:
        flash(f"Тест модели не пройден: {exc}", "error")
    return redirect(url_for("web.settings"))


@web_bp.post("/settings/ai/activate")
def activate_ai_model():
    try:
        _save_ai_settings_from_form()
        settings_value = get_model_manager().activate_selected_model()
        flash(f"Активирована модель: {settings_value.active_model_relative_path}.", "success")
    except (KeyError, ValueError, ModelLoadError) as exc:
        flash(f"Модель не активирована: {exc}", "error")
    return redirect(url_for("web.settings"))


@web_bp.post("/settings/ai/auto-select")
def auto_select_ai_model():
    try:
        _save_ai_settings_from_form()
        AISettingsService().save({"selection_mode": "auto"})
        settings_value = get_model_manager().ensure_auto_selection(force=True)
        if settings_value.active_model_relative_path:
            flash(
                f"Автоматически назначена модель: {settings_value.active_model_relative_path}. "
                "Она загрузится при первом AI-запросе.",
                "success",
            )
        else:
            flash("Подходящая локальная модель не найдена. Используется fallback.", "warning")
    except (ValueError, ModelLoadError) as exc:
        flash(f"Автоматический подбор не выполнен: {exc}", "error")
    return redirect(url_for("web.settings"))


@web_bp.post("/settings/ai/fallback")
def activate_ai_fallback():
    get_model_manager().set_fallback()
    flash("Локальная LLM отключена. Используется детерминированный fallback.", "info")
    return redirect(url_for("web.settings"))


@web_bp.post("/settings/ai/unload")
def unload_ai_model():
    get_model_manager().unload_model()
    flash("Локальная модель выгружена из памяти. При следующем AI-запросе она загрузится снова.", "success")
    return redirect(url_for("web.settings"))


@web_bp.post("/settings/ai/refresh")
def refresh_ai_models():
    count = len(ModelRegistry(current_app.config["AI_MODELS_DIR"]).refresh())
    settings_value = AISettingsService().get()
    if settings_value.selection_mode == "auto":
        get_model_manager().ensure_auto_selection(force=True)
    flash(f"Список моделей обновлён. Найдено: {count}.", "info")
    return redirect(url_for("web.settings"))


def _flash_training_summary(summary: dict, success_prefix: str = "Импорт завершён") -> None:
    added = summary.get("added", 0)
    skipped = summary.get("skipped", 0)
    errors = summary.get("errors", []) or []
    flash(f"{success_prefix}: добавлено {added}, пропущено дублей {skipped}.", "info")
    if errors:
        preview = "; ".join(str(err) for err in errors[:5])
        more = f" Ещё ошибок: {len(errors) - 5}." if len(errors) > 5 else ""
        flash(f"Предупреждения при импорте: {preview}.{more}", "error")


@web_bp.route("/training", methods=["GET", "POST"])
def training():
    rules_store = RulesStore()
    rules = rules_store.list_rules()
    examples_store = TrainingExamplesStore()

    if request.method == "POST":
        mode = request.form.get("mode", "single")
        rule_id = request.form.get("rule_id") or None
        prompt = request.form.get("prompt", "").strip() or None

        if mode == "batch_zip":
            archive = request.files.get("batch_zip")
            if not archive or archive.filename == "":
                flash("Выберите ZIP-архив с обучающими парами.", "error")
                return redirect(url_for("web.training"))
            if Path(archive.filename).suffix.lower() != ".zip":
                flash("Загрузите ZIP-архив с обучающими парами.", "error")
                return redirect(url_for("web.training"))
            with tempfile.TemporaryDirectory(prefix="training_upload_") as tmp:
                zip_path = Path(tmp) / _sanitize_filename(archive.filename)
                archive.save(zip_path)
                summary = examples_store.import_from_zip(zip_path, rule_id=rule_id, prompt=prompt)
            _flash_training_summary(summary, "Пакетное обучение из ZIP завершено")
            return redirect(url_for("web.training"))

        if mode == "batch_files":
            source_files = [f for f in request.files.getlist("source_files") if f and f.filename]
            target_files = [f for f in request.files.getlist("target_files") if f and f.filename]
            if not source_files or not target_files:
                flash("Выберите набор исходных и итоговых Excel-файлов.", "error")
                return redirect(url_for("web.training"))
            if any(not _is_excel_filename(f.filename) for f in source_files + target_files):
                flash("Для обучения можно загрузить только Excel-файлы .xlsx или .xls.", "error")
                return redirect(url_for("web.training"))
            with tempfile.TemporaryDirectory(prefix="training_files_") as tmp:
                tmp_dir = Path(tmp)
                source_dir = tmp_dir / "source"
                target_dir = tmp_dir / "target"
                source_dir.mkdir(parents=True, exist_ok=True)
                target_dir.mkdir(parents=True, exist_ok=True)
                for source in source_files:
                    source.save(source_dir / _sanitize_filename(source.filename))
                for target in target_files:
                    target.save(target_dir / _sanitize_filename(target.filename))
                summary = examples_store.import_from_directories(
                    source_dir=source_dir,
                    target_dir=target_dir,
                    rule_id=rule_id,
                    prompt=prompt,
                )
            _flash_training_summary(summary, "Пакетное обучение из файлов завершено")
            return redirect(url_for("web.training"))

        source_file = request.files.get("source_file")
        target_file = request.files.get("target_file")
        if not source_file or source_file.filename == "":
            flash("Выберите исходный файл.", "error")
            return redirect(url_for("web.training"))
        if not target_file or target_file.filename == "":
            flash("Выберите итоговый файл.", "error")
            return redirect(url_for("web.training"))
        if not _is_excel_filename(source_file.filename) or not _is_excel_filename(target_file.filename):
            flash("Для обучения можно загрузить только Excel-файлы .xlsx или .xls.", "error")
            return redirect(url_for("web.training"))
        with tempfile.TemporaryDirectory(prefix="training_single_") as tmp:
            temp_dir = Path(tmp)
            source_filename = _sanitize_filename(source_file.filename)
            target_filename = _sanitize_filename(target_file.filename)
            source_path = temp_dir / source_filename
            target_path = temp_dir / target_filename
            source_file.save(source_path)
            target_file.save(target_path)
            summary = examples_store.add_examples_from_pairs(
                [
                    {
                        "source_filename": source_filename,
                        "target_filename": target_filename,
                        "source_path": source_path,
                        "target_path": target_path,
                    }
                ],
                rule_id=rule_id,
                prompt=prompt,
            )
        _flash_training_summary(summary, "Обучающий пример сохранён")
        return redirect(url_for("web.training"))

    examples = list(reversed(examples_store.list_examples()))
    return render_template(
        "training.html",
        rules=rules,
        examples=examples[:100],
        training_stats=examples_store.get_stats(),
    )


@web_bp.route("/training/<example_id>/delete", methods=["POST"])
def delete_training_example(example_id: str):
    examples_store = TrainingExamplesStore()
    if examples_store.delete_example(example_id):
        flash("Обучающий пример удалён.", "info")
    else:
        flash("Обучающий пример не найден.", "error")
    return redirect(url_for("web.training"))


def _render_assistant_page(
    *,
    result: dict[str, Any] | None,
    raw_prompt: str = "",
    target_preview: dict[str, Any] | None = None,
    preview_error: str | None = None,
    conversion_diagnostics: list[dict[str, Any]] | None = None,
    draft_job: dict[str, Any] | None = None,
    source_preview: dict[str, Any] | None = None,
    prompt_tips: list[str] | None = None,
    selected_sheet: str | None = None,
):
    similar_rules = []
    if result:
        similar_rules = RulesStore().find_similar_rules((result.get("analysis") or {}).get("fingerprint") or {})
    diagnostics = conversion_diagnostics or []
    has_blocking_conversion_errors = any(item.get("severity") == "error" for item in diagnostics)
    return render_template(
        "assistant.html",
        result=result,
        similar_rules=similar_rules,
        generated_rule_json=json.dumps((result or {}).get("generated_rule") or {}, ensure_ascii=False),
        fingerprint_json=json.dumps(((result or {}).get("analysis") or {}).get("fingerprint") or {}, ensure_ascii=False),
        raw_prompt=raw_prompt,
        target_preview=target_preview,
        preview_error=preview_error,
        conversion_diagnostics=diagnostics,
        has_blocking_conversion_errors=has_blocking_conversion_errors,
        feedback_stats=FeedbackStore().get_stats(),
        draft_job=draft_job,
        source_preview=source_preview,
        prompt_tips=prompt_tips or [],
        selected_sheet=selected_sheet,
    )


def _preview_for_assistant(
    job_id: str,
    instruction: str,
    generated_rule: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    preview_result = ConversionService().preview_workbook_with_instruction(
        job_id=job_id,
        instruction=instruction,
        generated_rule=generated_rule,
        max_rows=60,
        max_columns=18,
    )
    diagnostics = preview_result.get("diagnostics") or []
    if "error" in preview_result:
        return None, preview_result.get("error"), diagnostics
    return preview_result.get("workbook_preview"), None, diagnostics


@web_bp.route("/assistant", methods=["GET", "POST"])
def instruction_assistant():
    """Resumable AI work session. Heavy analysis runs as a background job."""
    repository = JobsRepository()
    if request.method == "POST":
        job_id = request.form.get("job_id", "").strip()
        raw_prompt = request.form.get("raw_prompt", "").strip()
        sheet_name = request.form.get("sheet_name", "").strip() or None
        existing = repository.get(job_id) if job_id else None

        if not existing:
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("Выберите Excel-файл для анализа.", "error")
                return redirect(url_for("web.instruction_assistant"))
            if not _is_excel_filename(file.filename):
                flash("Загрузите файл Excel в формате .xlsx или .xls.", "error")
                return redirect(url_for("web.instruction_assistant"))
            try:
                job_id, filename, path = _save_uploaded_file(file)
                repository.update_context(
                    job_id,
                    job_kind="assistant",
                    selected_sheet=sheet_name,
                    resume_step=1,
                    original_instruction=raw_prompt or None,
                    assistant_state={"raw_prompt": raw_prompt, "selected_sheet": sheet_name, "current_step": 1},
                )
            except ValueError as exc:
                flash(f"Некорректный Excel-файл: {exc}", "error")
                return redirect(url_for("web.instruction_assistant"))
        elif existing.get("job_kind") != "assistant":
            flash("Эта задача не является рабочей сессией AI-помощника.", "error")
            return redirect(url_for("web.jobs_list"))

        queued = enqueue_assistant_analysis(
            job_id,
            raw_prompt=raw_prompt,
            sheet_name=sheet_name,
            revision_number=int((existing or {}).get("assistant_state", {}).get("revision_number") or 1),
        )
        if queued.get("status") == "failed":
            flash("Не удалось запустить анализ файла.", "error")
            return redirect(url_for("web.instruction_assistant", job_id=job_id))
        flash("Анализ запущен в фоне. Можно перейти в другой раздел и вернуться к задаче позже.", "success")
        return redirect(url_for("web.job_detail", job_id=job_id))

    job_id = request.args.get("job_id", "").strip()
    if not job_id:
        return _render_assistant_page(result=None)
    job = repository.get(job_id)
    if not job or job.get("job_kind") != "assistant":
        flash("Рабочая сессия AI-помощника не найдена.", "error")
        return redirect(url_for("web.instruction_assistant"))
    state = dict(job.get("assistant_state") or {})
    result = state.get("result")
    return _render_assistant_page(
        result=result,
        raw_prompt=state.get("raw_prompt") or job.get("original_instruction") or "",
        target_preview=state.get("target_preview"),
        preview_error=state.get("preview_error"),
        conversion_diagnostics=state.get("conversion_diagnostics") or [],
        draft_job=job,
        source_preview=state.get("source_workbook_preview"),
        prompt_tips=state.get("prompt_tips") or [],
        selected_sheet=state.get("selected_sheet") or job.get("selected_sheet"),
    )


@web_bp.route("/assistant/preview", methods=["POST"])
def assistant_preview_result():
    job_id = request.form.get("job_id", "").strip()
    raw_prompt = request.form.get("raw_prompt", "").strip()
    edited_instruction = request.form.get("prompt", "").strip()
    previous_ai_instruction = request.form.get("previous_ai_instruction", "").strip()
    revision_number = int(request.form.get("revision_number", "1") or 1)
    if not job_id or not edited_instruction:
        flash("Сначала загрузите файл и напишите инструкцию.", "error")
        return redirect(url_for("web.instruction_assistant"))
    job = JobsRepository().get(job_id)
    if not job or job.get("job_kind") != "assistant":
        flash("Рабочая сессия не найдена.", "error")
        return redirect(url_for("web.instruction_assistant"))
    queued = enqueue_assistant_analysis(
        job_id,
        raw_prompt=edited_instruction,
        sheet_name=job.get("selected_sheet"),
        previous_ai_instruction=previous_ai_instruction,
        revision_number=revision_number + 1,
    )
    if queued.get("status") == "failed":
        flash("Не удалось обновить анализ.", "error")
        return redirect(url_for("web.instruction_assistant", job_id=job_id))
    flash("Новая версия инструкции анализируется в фоне. Прогресс доступен в задаче.", "success")
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.route("/assistant/convert", methods=["POST"])
def assistant_convert_checked():
    """Поставить проверенную конвертацию AI-помощника в фоновую очередь."""
    job_id = request.form.get("job_id", "").strip()
    prompt = request.form.get("prompt", "").strip()
    generated_rule = _parse_json_field(request.form.get("generated_rule"), {}) or {}

    if not job_id or not prompt:
        flash("Сначала загрузите файл и сформируйте предпросмотр результата.", "error")
        return redirect(url_for("web.instruction_assistant"))

    service = ConversionService()
    source_path = service.file_manager.resolve_input(job_id)
    if not source_path.exists():
        flash("Исходный файл не найден. Загрузите файл ещё раз.", "error")
        return redirect(url_for("web.instruction_assistant"))

    repository = JobsRepository()
    current_job = repository.get(job_id) or {}
    state = dict(current_job.get("assistant_state") or {})
    state.update({
        "raw_prompt": request.form.get("raw_prompt", "").strip() or state.get("raw_prompt", ""),
        "improved_instruction": prompt,
        "generated_rule": generated_rule,
        "current_step": 7,
    })
    repository.update_context(
        job_id,
        job_kind="assistant",
        resume_step=7,
        original_instruction=state.get("raw_prompt") or prompt,
        improved_instruction=prompt,
        assistant_state=state,
    )
    queued_job = enqueue_conversion(
        job_id,
        instruction=prompt,
        generated_rule=generated_rule,
    )
    if queued_job.get("status") == "failed":
        flash("Не удалось запустить фоновую обработку.", "error")
        return redirect(url_for("web.instruction_assistant"))

    flash(
        "Финальная конвертация запущена в фоне. Можно закрыть страницу; "
        "статус и остановка доступны в разделе «Задачи».",
        "success",
    )
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.route("/assistant/save-rule", methods=["POST"])
def save_assistant_rule():
    name = request.form.get("name", "").strip()
    prompt = request.form.get("prompt", "").strip()
    raw_prompt = request.form.get("raw_prompt", "").strip()
    generated_rule = _parse_json_field(request.form.get("generated_rule"), {}) or {}
    fingerprint = _parse_json_field(request.form.get("fingerprint"), {}) or {}
    domain = request.form.get("domain", "universal").strip() or "universal"
    source_filename = request.form.get("source_filename", "").strip() or None

    if not name or not prompt:
        flash("Укажите название шаблона и итоговую инструкцию.", "error")
        return redirect(url_for("web.instruction_assistant"))

    rule = RulesStore().add_rule(
        name=name,
        prompt=prompt,
        raw_prompt=raw_prompt,
        generated_rule=_strip_request_only_report_config(generated_rule),
        fingerprint=fingerprint,
        description="Создано через AI-помощник. JSON-правило сформировано автоматически из инструкции.",
        domain=domain,
        use_raw_data=True,
        sheet_name=(generated_rule or {}).get("sheet_name"),
    )
    FeedbackStore().add_template_acceptance(
        source_filename=source_filename,
        raw_prompt=raw_prompt,
        accepted_instruction=prompt,
        generated_rule=generated_rule,
        analysis_fingerprint=fingerprint,
        rule_id=rule.get("id"),
    )
    flash(f"Шаблон '{rule['name']}' сохранён. Теперь его можно выбрать при конвертации.", "info")
    return redirect(url_for("web.rules_list"))


@web_bp.get("/rules")
def rules_list():
    store = RulesStore()
    rules = store.list_rules()
    return render_template("rules.html", rules=rules)


@web_bp.get("/rules/<rule_id>")
def rule_detail(rule_id: str):
    rule = RulesStore().get_rule(rule_id)
    if not rule:
        return render_template(
            "error.html",
            title="Шаблон не найден",
            details="Запрошенный шаблон отсутствует в библиотеке.",
            suggestion="Вернитесь к библиотеке или создайте новый шаблон.",
        ), 404
    return render_template(
        "rule_detail.html",
        rule=rule,
        generated_rule_json=json.dumps(rule.get("generated_rule") or {}, ensure_ascii=False, indent=2),
    )


@web_bp.get("/rules/<rule_id>/export")
def export_rule(rule_id: str):
    rule = RulesStore().get_rule(rule_id)
    if not rule:
        flash("Шаблон для экспорта не найден.", "error")
        return redirect(url_for("web.rules_list"))
    payload = json.dumps(rule, ensure_ascii=False, indent=2).encode("utf-8")
    return send_file(
        BytesIO(payload),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"sheetnorm_rule_{rule_id}.json",
    )


@web_bp.post("/rules/<rule_id>/duplicate")
def duplicate_rule(rule_id: str):
    rule = RulesStore().duplicate_rule(rule_id)
    if not rule:
        flash("Шаблон для копирования не найден.", "error")
    else:
        flash(f"Создана копия шаблона: {rule['name']}.", "success")
    return redirect(url_for("web.rules_list"))


@web_bp.get("/rules/export")
def export_rules():
    payload = json.dumps(RulesStore().list_rules(), ensure_ascii=False, indent=2).encode("utf-8")
    return send_file(
        BytesIO(payload),
        mimetype="application/json",
        as_attachment=True,
        download_name="sheetnorm_rules.json",
    )


@web_bp.post("/rules/import")
def import_rules():
    incoming = request.files.get("rules_file")
    if not incoming or incoming.filename == "":
        flash("Выберите JSON-файл с шаблонами.", "error")
        return redirect(url_for("web.rules_list"))
    try:
        payload = json.load(incoming.stream)
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            raise ValueError("корневой элемент должен быть объектом или массивом")
        store = RulesStore()
        added = 0
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            prompt = str(item.get("prompt") or item.get("ai_improved_instruction") or "").strip()
            if not name or not prompt:
                continue
            store.add_rule(
                name=name,
                prompt=prompt,
                raw_prompt=item.get("raw_user_prompt"),
                generated_rule=item.get("generated_rule") or {},
                fingerprint=item.get("fingerprint") or {},
                description=item.get("description"),
                domain=item.get("domain") or "universal",
                use_raw_data=item.get("use_raw_data"),
                sheet_name=item.get("sheet_name"),
                category=item.get("category"),
                table_type=item.get("table_type"),
                tags=item.get("tags") or [],
            )
            added += 1
        flash(f"Импортировано шаблонов: {added}.", "success")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        flash(f"Не удалось импортировать шаблоны: {exc}.", "error")
    return redirect(url_for("web.rules_list"))


@web_bp.route("/rules/new", methods=["GET", "POST"])
def new_rule():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        prompt = request.form.get("prompt", "").strip()
        raw_prompt = request.form.get("raw_user_prompt", "").strip() or prompt
        domain = request.form.get("domain", "universal").strip() or "universal"
        category = request.form.get("category", "universal").strip() or domain
        table_type = request.form.get("table_type", "flat").strip() or "flat"
        tags = [item.strip() for item in request.form.get("tags", "").split(",") if item.strip()]
        use_raw_data_checked = request.form.get("use_raw_data") == "1"
        sheet_name = request.form.get("sheet_name", "").strip() or None
        generated_rule, parse_warnings = _parse_rule_json(request.form.get("generated_rule"))
        if not name:
            flash("Укажите название правила.", "error")
            return redirect(url_for("web.new_rule"))
        if not prompt:
            flash("Опишите правило преобразования.", "error")
            return redirect(url_for("web.new_rule"))
        store = RulesStore()
        rule = store.add_rule(
            name=name,
            prompt=prompt,
            raw_prompt=raw_prompt,
            generated_rule=generated_rule,
            domain=domain,
            use_raw_data=use_raw_data_checked,
            sheet_name=sheet_name,
            category=category,
            table_type=table_type,
            tags=tags,
        )
        for warning in parse_warnings + list(rule.get("validation_warnings") or []):
            flash(warning, "warning")
        flash("Шаблон сохранён.", "success")
        return redirect(url_for("web.rules_list"))
    return render_template("rules_new.html")


@web_bp.route("/rules/<rule_id>/edit", methods=["GET", "POST"])
def edit_rule(rule_id: str):
    store = RulesStore()
    rule = store.get_rule(rule_id)
    if not rule:
        flash("Правило не найдено.", "error")
        return redirect(url_for("web.rules_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        prompt = request.form.get("prompt", "").strip()
        raw_prompt = request.form.get("raw_user_prompt", "").strip() or prompt
        use_raw_data_checked = request.form.get("use_raw_data") == "1"
        sheet_name = request.form.get("sheet_name", "").strip() or None
        domain = request.form.get("domain", "universal").strip() or "universal"
        category = request.form.get("category", rule.get("category") or domain).strip() or domain
        table_type = request.form.get("table_type", rule.get("table_type") or "flat").strip() or "flat"
        tags = [item.strip() for item in request.form.get("tags", "").split(",") if item.strip()]
        generated_rule, parse_warnings = _parse_rule_json(request.form.get("generated_rule"))
        if parse_warnings:
            generated_rule = rule.get("generated_rule") or {}
        if not name:
            flash("Укажите название правила.", "error")
            return redirect(url_for("web.edit_rule", rule_id=rule_id))
        if not prompt:
            flash("Опишите правило преобразования.", "error")
            return redirect(url_for("web.edit_rule", rule_id=rule_id))
        updated = store.update_rule(
            rule_id=rule_id,
            name=name,
            prompt=prompt,
            raw_user_prompt=raw_prompt,
            use_raw_data=use_raw_data_checked,
            sheet_name=sheet_name,
            domain=domain,
            generated_rule=generated_rule,
            category=category,
            table_type=table_type,
            tags=tags,
        )
        if updated:
            for warning in parse_warnings + list(updated.get("validation_warnings") or []):
                flash(warning, "warning")
            flash("Шаблон успешно обновлён.", "success")
            return redirect(url_for("web.rules_list"))
        flash("Ошибка при обновлении правила.", "error")
        return redirect(url_for("web.edit_rule", rule_id=rule_id))
    return render_template(
        "rules_edit.html",
        rule=rule,
        generated_rule_json=json.dumps(rule.get("generated_rule") or {}, ensure_ascii=False, indent=2),
    )


@web_bp.route("/rules/<rule_id>/delete", methods=["POST"])
def delete_rule(rule_id: str):
    store = RulesStore()
    rule = store.get_rule(rule_id)
    if not rule:
        flash("Правило не найдено.", "error")
        return redirect(url_for("web.rules_list"))
    rule_name = rule.get("name", "Правило")
    if store.delete_rule(rule_id):
        flash(f"Правило '{rule_name}' успешно удалено.", "info")
    else:
        flash("Ошибка при удалении правила.", "error")
    return redirect(url_for("web.rules_list"))
