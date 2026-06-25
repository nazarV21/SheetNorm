from __future__ import annotations

import json
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

from app.services.ai.instruction_assistant import InstructionAssistant
from app.services.conversion_service import ConversionService
from app.services.table_structure_analyzer import TableStructureAnalyzer
from app.services.rule_schema import normalize_declarative_rule
from app.utils.rules_store import RulesStore
from app.utils.training_examples_store import TrainingExamplesStore
from app.utils.feedback_store import FeedbackStore
from app.utils.jobs_repository import JobsRepository
from app.utils.uploads import is_excel_filename, sanitize_upload_name, save_excel_upload, validate_excel_file


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
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _strip_request_only_report_config(generated_rule: dict[str, Any]) -> dict[str, Any]:
    rule = normalize_declarative_rule(generated_rule or {})
    rule.pop("excel_report", None)
    rule.pop("user_intent", None)
    return rule


@web_bp.get("/")
def dashboard():
    rules = RulesStore().list_rules()
    processing_jobs = JobsRepository().list()
    history_path = Path(current_app.config["HISTORY_FILE"])
    try:
        history_items = json.loads(history_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        history_items = []
    training_stats = TrainingExamplesStore().get_stats()
    return render_template(
        "dashboard.html",
        rules_count=len(rules),
        conversions_count=len(processing_jobs) or len(history_items),
        training_stats=training_stats,
        ai_backend=current_app.config.get("AI_BACKEND", "fallback"),
    )


@web_bp.post("/")
def legacy_index_post():
    return index()


@web_bp.route("/convert", methods=["GET", "POST"])
def index():
    rules_store = RulesStore()
    rules = rules_store.list_rules()

    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Пожалуйста, выберите файл Excel.", "error")
            return redirect(url_for("web.index"))
        if not _is_excel_filename(file.filename):
            flash("Загрузите файл Excel в формате .xlsx или .xls.", "error")
            return redirect(url_for("web.index"))

        mode = request.form.get("mode", "rule")
        rule_id = request.form.get("rule_id") or None
        custom_instruction = request.form.get("custom_instruction", "").strip()
        should_improve = request.form.get("improve_instruction") == "1"
        save_as_rule = request.form.get("save_as_rule") == "1"
        new_rule_name = request.form.get("new_rule_name", "").strip()

        try:
            job_id, _, target_path = _save_uploaded_file(file)
        except ValueError as exc:
            flash(f"Некорректный Excel-файл: {exc}", "error")
            return redirect(url_for("web.index"))
        service = ConversionService()

        instruction_details = None
        if mode == "custom" and custom_instruction:
            assistant_result = None
            instruction = custom_instruction
            generated_rule: dict[str, Any] = {}
            fingerprint: dict[str, Any] = {}
            if should_improve or save_as_rule:
                assistant_result = InstructionAssistant().prepare_instruction(target_path, custom_instruction)
                instruction = assistant_result["ai_improved_instruction"]
                generated_rule = assistant_result.get("generated_rule") or {}
                fingerprint = (assistant_result.get("analysis") or {}).get("fingerprint") or {}
                instruction_details = {
                    "raw_prompt": assistant_result.get("user_raw_prompt"),
                    "improved_instruction": assistant_result.get("ai_improved_instruction"),
                    "how_ai_understood": assistant_result.get("how_ai_understood") or [],
                    "instruction_changes": assistant_result.get("instruction_changes") or [],
                    "engine": assistant_result.get("engine"),
                }

            saved_rule = None
            if save_as_rule:
                if not new_rule_name:
                    new_rule_name = f"Шаблон для {Path(file.filename).stem}"
                saved_rule = rules_store.add_rule(
                    name=new_rule_name,
                    prompt=instruction,
                    raw_prompt=custom_instruction,
                    generated_rule=_strip_request_only_report_config(generated_rule),
                    fingerprint=fingerprint,
                    description="Создано из инструкции пользователя при загрузке файла",
                    domain=request.form.get("domain") or "universal",
                    use_raw_data=True,
                    sheet_name=generated_rule.get("sheet_name"),
                )
                flash(f"Инструкция сохранена как правило: {saved_rule['name']}", "info")

            result = service.convert_with_instruction(job_id=job_id, instruction=instruction, generated_rule=generated_rule)
        else:
            result = service.convert_with_rule(job_id=job_id, rule_id=rule_id, options={})

        if "error" in result:
            flash(result["error"], "error")
            return redirect(url_for("web.index"))

        output_name = result.get("output_filename") or Path(result["output"]).name
        return render_template(
            "result.html",
            job_id=result["job_id"],
            rows=result["rows"],
            columns=result.get("columns"),
            source_filename=result.get("source_filename"),
            output_filename=output_name,
            format="Excel",
            rule_name=result.get("rule_name"),
            instruction_details=instruction_details,
            quality_report=result.get("quality_report"),
        )

    return render_template("index.html", rules=rules, selected_rule_id=request.args.get("rule_id", ""))


@web_bp.get("/about")
def about():
    return render_template("about.html")


@web_bp.get("/deployment")
def deployment():
    return render_template("deployment.html")


@web_bp.get("/jobs")
def jobs_list():
    selected_status = request.args.get("status", "").strip()
    query = request.args.get("q", "").strip().lower()
    jobs = JobsRepository().list(status=selected_status or None)
    if query:
        jobs = [
            job for job in jobs
            if query in str(job.get("input_filename") or "").lower()
            or query in str(job.get("rule_name") or "").lower()
            or query in str(job.get("job_id") or "").lower()
        ]
    return render_template(
        "jobs.html",
        jobs=jobs,
        selected_status=selected_status,
        query=request.args.get("q", ""),
    )


@web_bp.get("/jobs/<job_id>")
def job_detail(job_id: str):
    job = JobsRepository().get(job_id)
    if not job:
        return render_template(
            "error.html",
            title="Задача не найдена",
            details=f"Задача {job_id} отсутствует или была удалена.",
            suggestion="Вернитесь к списку задач или загрузите файл повторно.",
        ), 404

    preview = None
    preview_error = None
    output_filename = job.get("output_filename")
    output_path_value = job.get("output_path")
    if output_path_value:
        output_dir = Path(current_app.config["OUTPUT_DIR"]).resolve()
        output_path = Path(output_path_value).resolve()
        if output_path.parent == output_dir and output_path.exists():
            try:
                preview_df = pd.read_excel(output_path, sheet_name="Результат", nrows=30)
                preview = ConversionService.dataframe_to_preview(preview_df, max_rows=30)
            except Exception as exc:
                preview_error = f"Предпросмотр результата недоступен: {exc}"
        else:
            preview_error = "Файл результата отсутствует в каталоге output."
    return render_template(
        "job_detail.html",
        job=job,
        quality_report=job.get("quality_report") or {},
        preview=preview,
        preview_error=preview_error,
        output_filename=output_filename,
    )


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
    history_path = Path(current_app.config["HISTORY_FILE"])
    try:
        items = json.loads(history_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        items = []
    items = list(reversed(items))
    return render_template("history.html", items=items)


@web_bp.route("/history/clear", methods=["POST"])
def clear_history():
    history_path = Path(current_app.config["HISTORY_FILE"])
    try:
        history_path.write_text(json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
        flash("История успешно очищена.", "info")
    except Exception as e:
        flash(f"Ошибка при очистке истории: {str(e)}", "error")
    return redirect(url_for("web.history"))


@web_bp.get("/settings")
def settings():
    examples_store = TrainingExamplesStore()
    return render_template(
        "settings.html",
        config=current_app.config,
        training_stats=examples_store.get_stats(),
    )


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
    )


def _preview_for_assistant(
    job_id: str,
    instruction: str,
    generated_rule: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    preview_result = ConversionService().preview_with_instruction(
        job_id=job_id,
        instruction=instruction,
        generated_rule=generated_rule,
        max_rows=80,
    )
    diagnostics = preview_result.get("diagnostics") or []
    if "error" in preview_result:
        return None, preview_result.get("error"), diagnostics
    return preview_result.get("preview"), None, diagnostics


@web_bp.route("/assistant", methods=["GET", "POST"])
def instruction_assistant():
    """AI-мастер: пользователь пишет только инструкцию, система делает остальное."""
    if request.method == "POST":
        file = request.files.get("file")
        raw_prompt = request.form.get("raw_prompt", "").strip()
        sheet_name = request.form.get("sheet_name", "").strip() or None
        if not file or file.filename == "":
            flash("Выберите Excel-файл для анализа.", "error")
            return redirect(url_for("web.instruction_assistant"))
        if not _is_excel_filename(file.filename):
            flash("Загрузите файл Excel в формате .xlsx или .xls.", "error")
            return redirect(url_for("web.instruction_assistant"))

        try:
            job_id, filename, path = _save_uploaded_file(file)
        except ValueError as exc:
            flash(f"Некорректный Excel-файл: {exc}", "error")
            return redirect(url_for("web.instruction_assistant"))
        assistant = InstructionAssistant()
        result = assistant.prepare_instruction(path, raw_prompt, sheet_name=sheet_name)
        result["job_id"] = job_id
        result["source_filename"] = filename
        result["revision_number"] = 1

        target_preview = None
        preview_error = None
        conversion_diagnostics: list[dict[str, Any]] = []
        if result.get("ready_for_preview"):
            target_preview, preview_error, conversion_diagnostics = _preview_for_assistant(
                job_id,
                result.get("ai_improved_instruction") or raw_prompt,
                result.get("generated_rule") or {},
            )
        return _render_assistant_page(
            result=result,
            raw_prompt=raw_prompt,
            target_preview=target_preview,
            preview_error=preview_error,
            conversion_diagnostics=conversion_diagnostics,
        )
    return _render_assistant_page(result=None)


@web_bp.route("/assistant/preview", methods=["POST"])
def assistant_preview_result():
    """Переанализировать Excel после изменения инструкции и обновить предпросмотр."""
    job_id = request.form.get("job_id", "").strip()
    raw_prompt = request.form.get("raw_prompt", "").strip()
    edited_instruction = request.form.get("prompt", "").strip()
    previous_ai_instruction = request.form.get("previous_ai_instruction", "").strip()
    source_filename = request.form.get("source_filename", "").strip()
    revision_number = int(request.form.get("revision_number", "1") or 1)

    if not job_id or not edited_instruction:
        flash("Сначала загрузите файл и напишите инструкцию.", "error")
        return redirect(url_for("web.instruction_assistant"))

    service = ConversionService()
    source_path = service.file_manager.resolve_input(job_id)
    if not source_path.exists():
        flash("Исходный файл для повторного анализа не найден. Загрузите файл ещё раз.", "error")
        return redirect(url_for("web.instruction_assistant"))

    assistant = InstructionAssistant()
    result = assistant.prepare_instruction(
        source_path,
        edited_instruction,
        previous_ai_instruction=previous_ai_instruction,
    )
    result["job_id"] = job_id
    result["source_filename"] = source_filename or service.file_manager.get_original_filename(job_id) or source_path.name
    result["revision_number"] = revision_number + 1

    target_preview, preview_error, conversion_diagnostics = _preview_for_assistant(
        job_id,
        result.get("ai_improved_instruction") or edited_instruction,
        result.get("generated_rule") or {},
    )

    # Фиксируем каждую правку инструкции как обратную связь. Пользователь не правит JSON и таблицу;
    # система запоминает, что прежняя инструкция была уточнена текстом пользователя.
    if previous_ai_instruction and previous_ai_instruction.strip() != edited_instruction.strip():
        FeedbackStore().add_instruction_revision(
            source_filename=result.get("source_filename"),
            raw_prompt=raw_prompt,
            previous_ai_instruction=previous_ai_instruction,
            user_corrected_instruction=edited_instruction,
            regenerated_instruction=result.get("ai_improved_instruction") or edited_instruction,
            generated_rule=result.get("generated_rule") or {},
            analysis_fingerprint=(result.get("analysis") or {}).get("fingerprint") or {},
            preview_columns=(target_preview or {}).get("columns") or [],
            preview_rows_count=(target_preview or {}).get("total_rows") or (target_preview or {}).get("shown_rows") or 0,
            notes="Пользователь изменил инструкцию; система заново проанализировала Excel и пересобрала правило.",
        )

    return _render_assistant_page(
        result=result,
        raw_prompt=raw_prompt,
        target_preview=target_preview,
        preview_error=preview_error,
        conversion_diagnostics=conversion_diagnostics,
    )


@web_bp.route("/assistant/convert", methods=["POST"])
def assistant_convert_checked():
    """Финальная конвертация после проверки предпросмотра.

    Если при применении инструкции возникла ошибка, пользователь возвращается в
    AI-помощник, видит диагностику и меняет только текст инструкции.
    """
    job_id = request.form.get("job_id", "").strip()
    prompt = request.form.get("prompt", "").strip()
    raw_prompt = request.form.get("raw_prompt", "").strip()
    source_filename = request.form.get("source_filename", "").strip()
    generated_rule = _parse_json_field(request.form.get("generated_rule"), {}) or {}

    if not job_id or not prompt:
        flash("Сначала загрузите файл и сформируйте предпросмотр результата.", "error")
        return redirect(url_for("web.instruction_assistant"))

    service = ConversionService()
    source_path = service.file_manager.resolve_input(job_id)
    if not source_path.exists():
        flash("Исходный файл не найден. Загрузите файл ещё раз.", "error")
        return redirect(url_for("web.instruction_assistant"))

    conversion = service.convert_with_instruction_checked(
        job_id=job_id,
        instruction=prompt,
        generated_rule=generated_rule,
    )
    if "error" in conversion:
        assistant = InstructionAssistant()
        result = assistant.prepare_instruction(source_path, prompt, previous_ai_instruction=prompt)
        result["job_id"] = job_id
        result["source_filename"] = source_filename or service.file_manager.get_original_filename(job_id) or source_path.name
        result["revision_number"] = int(request.form.get("revision_number", "1") or 1)
        target_preview = None
        preview_error = conversion.get("error")
        conversion_diagnostics = conversion.get("diagnostics") or []
        FeedbackStore().add_instruction_revision(
            source_filename=result.get("source_filename"),
            raw_prompt=raw_prompt,
            previous_ai_instruction=prompt,
            user_corrected_instruction=prompt,
            regenerated_instruction=result.get("ai_improved_instruction") or prompt,
            generated_rule=result.get("generated_rule") or {},
            analysis_fingerprint=(result.get("analysis") or {}).get("fingerprint") or {},
            preview_columns=[],
            preview_rows_count=0,
            notes="Финальная конвертация остановлена: система обнаружила ошибку применения инструкции.",
        )
        return _render_assistant_page(
            result=result,
            raw_prompt=raw_prompt,
            target_preview=target_preview,
            preview_error=preview_error,
            conversion_diagnostics=conversion_diagnostics,
        )

    output_name = conversion.get("output_filename") or Path(conversion["output"]).name
    return render_template(
        "result.html",
        job_id=conversion["job_id"],
        rows=conversion["rows"],
        columns=conversion.get("columns"),
        source_filename=conversion.get("source_filename"),
        output_filename=output_name,
        format="Excel",
        rule_name=conversion.get("rule_name"),
        instruction_details={
            "raw_prompt": raw_prompt,
            "improved_instruction": prompt,
            "how_ai_understood": [],
            "instruction_changes": [],
            "engine": "AI-помощник + проверенная конвертация",
        },
        quality_report=conversion.get("quality_report"),
    )


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
