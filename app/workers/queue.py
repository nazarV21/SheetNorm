from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app, has_app_context

from app.services.ai.instruction_assistant import InstructionAssistant
from app.services.conversion_service import ConversionService
from app.services.workbook_preview import build_workbook_preview
from app.utils.feedback_store import FeedbackStore
from app.utils.jobs_repository import JobsRepository
from app.utils.rules_store import RulesStore
from app.workers.local_tasks import get_local_task_manager


TERMINAL_STATUSES = {"success", "failed", "cancelled"}
ACTIVE_STATUSES = {"created", "queued", "processing", "requires_review"}


class JobCancelledError(RuntimeError):
    pass


def get_redis_connection():
    from redis import Redis

    return Redis.from_url(current_app.config["REDIS_URL"])


def get_queue():
    from rq import Queue

    return Queue(current_app.config.get("RQ_QUEUE_NAME", "sheetnorm"), connection=get_redis_connection())


def _is_cancelled(job_id: str, cancel_event: threading.Event | None = None) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        return True
    job = JobsRepository().get(job_id)
    return bool(job and job.get("status") == "cancelled")


def _ensure_not_cancelled(job_id: str, cancel_event: threading.Event | None = None) -> None:
    if _is_cancelled(job_id, cancel_event):
        raise JobCancelledError(f"Processing job {job_id} was cancelled.")



def _dispatch_background(job_id: str, function_path: str, function, payload: dict[str, Any]) -> dict[str, Any]:
    repository = JobsRepository()
    existing = repository.get(job_id)
    if not existing:
        return {"job_id": job_id, "status": "failed"}
    if existing.get("status") in {"queued", "processing"}:
        return existing
    if existing.get("status") == "cancelled":
        return existing

    queued = repository.update_status(job_id, "queued", stage="queued", progress=0) or existing
    async_mode = str(current_app.config.get("ASYNC_MODE", "thread")).lower()
    if async_mode == "rq":
        queue_job_id = f"sheetnorm-{uuid.uuid4()}"
        repository.update_status(job_id, "queued", stage="queued", progress=0, queue_job_id=queue_job_id)
        try:
            get_queue().enqueue(
                function_path,
                job_id,
                **payload,
                job_id=queue_job_id,
                job_timeout=current_app.config.get("RQ_DEFAULT_TIMEOUT", 600),
                result_ttl=current_app.config.get("RQ_RESULT_TTL", 86400),
                failure_ttl=current_app.config.get("RQ_FAILURE_TTL", 604800),
            )
        except Exception as exc:
            current_app.logger.exception("Could not enqueue RQ job", extra={"job_id": job_id})
            repository.append_error(job_id, "Не удалось поставить задачу в очередь.", code="QUEUE_UNAVAILABLE", details=str(exc))
        return repository.get(job_id) or queued

    if async_mode in {"thread", "background", "local"}:
        queue_job_id = f"thread:{job_id}"
        repository.update_status(job_id, "queued", stage="queued", progress=0, queue_job_id=queue_job_id)
        app = current_app._get_current_object()
        manager = get_local_task_manager(app, max_workers=int(current_app.config.get("LOCAL_WORKER_THREADS", 2)))
        manager.submit(app, job_id, function, job_id, **payload)
        return repository.get(job_id) or queued

    function(job_id, **payload)
    return repository.get(job_id) or queued


def enqueue_assistant_analysis(
    job_id: str,
    *,
    raw_prompt: str,
    sheet_name: str | None = None,
    previous_ai_instruction: str | None = None,
    revision_number: int = 1,
) -> dict[str, Any]:
    repository = JobsRepository()
    existing = repository.get(job_id)
    if not existing:
        return {"job_id": job_id, "status": "failed"}
    state = dict(existing.get("assistant_state") or {})
    state.update({
        "raw_prompt": raw_prompt,
        "selected_sheet": sheet_name,
        "previous_ai_instruction": previous_ai_instruction or "",
        "revision_number": int(revision_number or 1),
        "current_step": 2,
    })
    repository.update_context(
        job_id,
        job_kind="assistant",
        selected_sheet=sheet_name,
        resume_step=2,
        original_instruction=raw_prompt,
        assistant_state=state,
    )
    return _dispatch_background(
        job_id,
        "app.workers.queue.run_assistant_analysis_job",
        run_assistant_analysis_job,
        {
            "raw_prompt": raw_prompt,
            "sheet_name": sheet_name,
            "previous_ai_instruction": previous_ai_instruction,
            "revision_number": int(revision_number or 1),
        },
    )


def run_assistant_analysis_job(
    job_id: str,
    *,
    raw_prompt: str,
    sheet_name: str | None = None,
    previous_ai_instruction: str | None = None,
    revision_number: int = 1,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    if not has_app_context():
        from app import create_app

        app = create_app()
        with app.app_context():
            return _run_assistant_analysis_job(
                job_id,
                raw_prompt=raw_prompt,
                sheet_name=sheet_name,
                previous_ai_instruction=previous_ai_instruction,
                revision_number=revision_number,
                cancel_event=cancel_event,
            )
    return _run_assistant_analysis_job(
        job_id,
        raw_prompt=raw_prompt,
        sheet_name=sheet_name,
        previous_ai_instruction=previous_ai_instruction,
        revision_number=revision_number,
        cancel_event=cancel_event,
    )


def _run_assistant_analysis_job(
    job_id: str,
    *,
    raw_prompt: str,
    sheet_name: str | None,
    previous_ai_instruction: str | None,
    revision_number: int,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    repository = JobsRepository()
    try:
        _ensure_not_cancelled(job_id, cancel_event)
        repository.update_status(job_id, "processing", stage="analyzing_file", progress=10)
        service = ConversionService(cancel_event=cancel_event)
        source_path = service.file_manager.resolve_input(job_id)
        if not source_path.exists():
            raise FileNotFoundError("Исходный файл рабочей сессии не найден.")

        source_preview = build_workbook_preview(source_path, max_rows=40, max_columns=18)
        source_preview["filename"] = repository.get(job_id).get("input_filename") or source_path.name
        _ensure_not_cancelled(job_id, cancel_event)
        repository.update_status(job_id, "processing", stage="improving_instruction", progress=35)
        result = InstructionAssistant().prepare_instruction(
            source_path,
            raw_prompt,
            sheet_name=sheet_name,
            previous_ai_instruction=previous_ai_instruction,
        )
        result["job_id"] = job_id
        result["source_filename"] = source_preview["filename"]
        result["revision_number"] = int(revision_number or 1)
        result["source_workbook_preview"] = source_preview

        target_preview = None
        preview_error = None
        diagnostics: list[dict[str, Any]] = []
        if result.get("ready_for_preview"):
            _ensure_not_cancelled(job_id, cancel_event)
            repository.update_status(job_id, "processing", stage="building_preview", progress=65)
            preview_result = service.preview_workbook_with_instruction(
                job_id=job_id,
                instruction=result.get("ai_improved_instruction") or raw_prompt,
                generated_rule=result.get("generated_rule") or {},
                max_rows=60,
                max_columns=18,
            )
            diagnostics = list(preview_result.get("diagnostics") or [])
            preview_error = preview_result.get("error")
            target_preview = preview_result.get("workbook_preview")

        if previous_ai_instruction and previous_ai_instruction.strip() != (result.get("ai_improved_instruction") or raw_prompt).strip():
            FeedbackStore().add_instruction_revision(
                source_filename=result.get("source_filename"),
                raw_prompt=raw_prompt,
                previous_ai_instruction=previous_ai_instruction,
                user_corrected_instruction=raw_prompt,
                regenerated_instruction=result.get("ai_improved_instruction") or raw_prompt,
                generated_rule=result.get("generated_rule") or {},
                analysis_fingerprint=(result.get("analysis") or {}).get("fingerprint") or {},
                notes="Пользователь продолжил рабочую сессию и обновил инструкцию.",
            )

        existing_state = dict((repository.get(job_id) or {}).get("assistant_state") or {})
        state = {
            **existing_state,
            "raw_prompt": raw_prompt,
            "selected_sheet": sheet_name,
            "previous_ai_instruction": previous_ai_instruction or "",
            "revision_number": int(revision_number or 1),
            "result": result,
            "source_workbook_preview": source_preview,
            "target_preview": target_preview,
            "preview_error": preview_error,
            "conversion_diagnostics": diagnostics,
            "analysis_summary": existing_state.get("analysis_summary") or {
                "sheets": (result.get("analysis") or {}).get("sheets") or [],
                "selected_sheet": (result.get("analysis") or {}).get("selected_sheet"),
                "header_rows_human": (result.get("analysis") or {}).get("header_rows_human") or [],
                "data_start_row_human": (result.get("analysis") or {}).get("data_start_row_human"),
                "table_type": (result.get("analysis") or {}).get("table_type"),
            },
            "current_step": 6 if target_preview else 4,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state = json.loads(json.dumps(state, ensure_ascii=False, default=str))
        repository.update_context(
            job_id,
            job_kind="assistant",
            selected_sheet=sheet_name,
            execution_mode=(result.get("generated_rule") or {}).get("execution_mode") or "declarative_rule",
            resume_step=state["current_step"],
            original_instruction=raw_prompt,
            improved_instruction=result.get("ai_improved_instruction"),
            assistant_state=state,
        )
        repository.update_status(job_id, "requires_review", stage="waiting_for_review", progress=100)
        return {"job_id": job_id, "status": "requires_review"}
    except JobCancelledError:
        repository.update_status(job_id, "cancelled", stage="cancelled")
        return {"job_id": job_id, "status": "cancelled"}
    except Exception as exc:
        current_app.logger.exception("Assistant analysis failed", extra={"job_id": job_id})
        current = repository.get(job_id) or {}
        state = dict(current.get("assistant_state") or {})
        state.update({"raw_prompt": raw_prompt, "selected_sheet": sheet_name, "current_step": 2, "last_error": str(exc)})
        repository.update_context(job_id, job_kind="assistant", assistant_state=state, resume_step=2)
        repository.append_error(job_id, "AI-анализ завершился ошибкой.", code="ASSISTANT_ANALYSIS_FAILED", details=str(exc))
        return {"job_id": job_id, "status": "failed", "error": str(exc)}

def enqueue_conversion(
    job_id: str,
    *,
    rule_id: str | None = None,
    instruction: str | None = None,
    generated_rule: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    improve_instruction: bool = False,
    save_as_rule: bool = False,
    new_rule_name: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    repository = JobsRepository()
    existing = repository.get(job_id)
    if not existing:
        return {"job_id": job_id, "status": "failed"}
    if existing.get("status") in {"queued", "processing"}:
        return existing
    if existing.get("status") == "cancelled":
        return existing

    payload = {
        "rule_id": rule_id,
        "instruction": instruction,
        "generated_rule": generated_rule or {},
        "schema": schema,
        "options": options or {},
        "improve_instruction": bool(improve_instruction),
        "save_as_rule": bool(save_as_rule),
        "new_rule_name": new_rule_name,
        "domain": domain,
    }
    queued = repository.update_status(
        job_id,
        "queued",
        stage="queued",
        progress=0,
        rule_id=rule_id,
        original_instruction=instruction,
    ) or existing

    async_mode = str(current_app.config.get("ASYNC_MODE", "thread")).lower()
    if async_mode == "rq":
        queue_job_id = f"sheetnorm-{uuid.uuid4()}"
        queued = repository.update_status(
            job_id,
            "queued",
            stage="queued",
            progress=0,
            queue_job_id=queue_job_id,
        ) or queued
        try:
            get_queue().enqueue(
                "app.workers.queue.run_conversion_job",
                job_id,
                **payload,
                job_id=queue_job_id,
                job_timeout=current_app.config.get("RQ_DEFAULT_TIMEOUT", 600),
                result_ttl=current_app.config.get("RQ_RESULT_TTL", 86400),
                failure_ttl=current_app.config.get("RQ_FAILURE_TTL", 604800),
            )
        except Exception as exc:
            current_app.logger.exception("Could not enqueue RQ job", extra={"job_id": job_id})
            repository.append_error(job_id, "Не удалось поставить задачу в очередь.", code="QUEUE_UNAVAILABLE", details=str(exc))
        return repository.get(job_id) or queued

    if async_mode in {"thread", "background", "local"}:
        queue_job_id = f"thread:{job_id}"
        queued = repository.update_status(
            job_id,
            "queued",
            stage="queued",
            progress=0,
            queue_job_id=queue_job_id,
        ) or queued
        app = current_app._get_current_object()
        manager = get_local_task_manager(
            app,
            max_workers=int(current_app.config.get("LOCAL_WORKER_THREADS", 2)),
        )
        manager.submit(app, job_id, run_conversion_job, job_id, **payload)
        return repository.get(job_id) or queued

    # Explicit synchronous mode is kept for tests and debugging only.
    result = run_conversion_job(job_id, **payload)
    return repository.get(job_id) or {
        "job_id": job_id,
        "status": "success" if "error" not in result else "failed",
    }


def run_conversion_job(
    job_id: str,
    *,
    rule_id: str | None = None,
    instruction: str | None = None,
    generated_rule: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    improve_instruction: bool = False,
    save_as_rule: bool = False,
    new_rule_name: str | None = None,
    domain: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    if not has_app_context():
        # RQ imports this function in a standalone worker process. Build the
        # application context here so repositories and services can use config/DB.
        from app import create_app

        app = create_app()
        with app.app_context():
            return _run_conversion_job(
                job_id,
                rule_id=rule_id,
                instruction=instruction,
                generated_rule=generated_rule,
                schema=schema,
                options=options,
                improve_instruction=improve_instruction,
                save_as_rule=save_as_rule,
                new_rule_name=new_rule_name,
                domain=domain,
                cancel_event=cancel_event,
            )
    return _run_conversion_job(
        job_id,
        rule_id=rule_id,
        instruction=instruction,
        generated_rule=generated_rule,
        schema=schema,
        options=options,
        improve_instruction=improve_instruction,
        save_as_rule=save_as_rule,
        new_rule_name=new_rule_name,
        domain=domain,
        cancel_event=cancel_event,
    )


def _run_conversion_job(
    job_id: str,
    *,
    rule_id: str | None = None,
    instruction: str | None = None,
    generated_rule: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    improve_instruction: bool = False,
    save_as_rule: bool = False,
    new_rule_name: str | None = None,
    domain: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    repository = JobsRepository()
    try:
        _ensure_not_cancelled(job_id, cancel_event)
        repository.update_status(job_id, "processing", stage="preparing", progress=5)
        service = ConversionService(cancel_event=cancel_event)

        if instruction:
            final_instruction = instruction
            final_rule = generated_rule or {}
            saved_rule = None

            if improve_instruction or save_as_rule:
                _ensure_not_cancelled(job_id, cancel_event)
                repository.update_status(
                    job_id,
                    "processing",
                    stage="analyzing_instruction",
                    progress=15,
                )
                source_path = service.file_manager.resolve_input(job_id)
                assistant_result = InstructionAssistant().prepare_instruction(source_path, instruction)
                final_instruction = assistant_result.get("ai_improved_instruction") or instruction
                final_rule = assistant_result.get("generated_rule") or final_rule

                if save_as_rule:
                    _ensure_not_cancelled(job_id, cancel_event)
                    fingerprint = (assistant_result.get("analysis") or {}).get("fingerprint") or {}
                    rule_name = (new_rule_name or "").strip() or f"Шаблон для {Path(source_path).stem}"
                    saved_rule = RulesStore().add_rule(
                        name=rule_name,
                        prompt=final_instruction,
                        raw_prompt=instruction,
                        generated_rule=final_rule,
                        fingerprint=fingerprint,
                        description="Создано из инструкции пользователя при фоновой обработке",
                        domain=(domain or "universal"),
                        use_raw_data=True,
                        sheet_name=final_rule.get("sheet_name"),
                    )
                    repository.update_status(
                        job_id,
                        "processing",
                        stage="template_saved",
                        progress=25,
                        rule_id=saved_rule.get("id"),
                        rule_name=saved_rule.get("name"),
                        improved_instruction=final_instruction,
                    )

            _ensure_not_cancelled(job_id, cancel_event)
            repository.update_status(
                job_id,
                "processing",
                stage="executing_transformation",
                progress=35,
                original_instruction=instruction,
                improved_instruction=final_instruction,
                rule_id=(saved_rule or {}).get("id"),
                rule_name=(saved_rule or {}).get("name"),
            )
            result = service.convert_with_instruction_checked(
                job_id,
                final_instruction,
                final_rule,
                options=options or {},
            )
        elif schema is not None:
            _ensure_not_cancelled(job_id, cancel_event)
            repository.update_status(
                job_id,
                "processing",
                stage="executing_transformation",
                progress=35,
            )
            result = service.convert(job_id, schema=schema, options=options or {})
        else:
            _ensure_not_cancelled(job_id, cancel_event)
            repository.update_status(
                job_id,
                "processing",
                stage="executing_transformation",
                progress=35,
                rule_id=rule_id,
            )
            result = service.convert_with_rule(job_id, rule_id, options=options or {})

        if _is_cancelled(job_id, cancel_event):
            return {"job_id": job_id, "status": "cancelled", "code": "JOB_CANCELLED"}
        current = repository.get(job_id) or {}
        if current.get("job_kind") == "assistant" and isinstance(result, dict) and "error" not in result:
            state = dict(current.get("assistant_state") or {})
            state.update({
                "current_step": 8,
                "completed": True,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
            repository.update_context(job_id, resume_step=8, assistant_state=state)
        return result
    except JobCancelledError:
        repository.update_status(job_id, "cancelled", stage="cancelled")
        return {"job_id": job_id, "status": "cancelled", "code": "JOB_CANCELLED"}
    except Exception as exc:
        current_app.logger.exception("Background conversion failed", extra={"job_id": job_id})
        current = repository.get(job_id)
        if current and current.get("status") == "cancelled":
            return {"job_id": job_id, "status": "cancelled", "code": "JOB_CANCELLED"}
        repository.append_error(job_id, "Фоновая обработка завершилась ошибкой.", details=str(exc))
        return {
            "job_id": job_id,
            "status": "failed",
            "error": "Фоновая обработка завершилась ошибкой.",
            "details": str(exc),
            "code": "BACKGROUND_JOB_FAILED",
        }


def cancel_conversion(job_id: str) -> dict[str, Any] | None:
    repository = JobsRepository()
    job = repository.get(job_id)
    if not job:
        return None
    if job.get("status") in TERMINAL_STATUSES:
        return job

    cancelled = repository.update_status(
        job_id,
        "cancelled",
        stage="cancelled",
        progress=int(job.get("progress") or 0),
    )

    async_mode = str(current_app.config.get("ASYNC_MODE", "thread")).lower()
    queue_job_id = str(job.get("queue_job_id") or "")
    if async_mode == "rq" and queue_job_id:
        try:
            from rq import cancel_job as rq_cancel_job

            connection = get_redis_connection()
            rq_cancel_job(queue_job_id, connection=connection)
            try:
                from rq.command import send_stop_job_command

                send_stop_job_command(connection, queue_job_id)
            except Exception:
                # A queued job is already cancelled by rq_cancel_job. A running
                # job may finish its current pandas operation, but the conversion
                # service checks the cancelled status before saving the result.
                pass
        except Exception:
            current_app.logger.exception("Could not cancel RQ job", extra={"job_id": job_id})
    elif async_mode in {"thread", "background", "local"}:
        manager = current_app.extensions.get("sheetnorm_local_tasks")
        if manager is not None:
            manager.cancel(job_id)

    return cancelled or repository.get(job_id)
