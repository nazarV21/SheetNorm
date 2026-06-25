from __future__ import annotations

from typing import Any

from flask import current_app

from app.services.conversion_service import ConversionService
from app.utils.jobs_repository import JobsRepository


def get_redis_connection():
    from redis import Redis

    return Redis.from_url(current_app.config["REDIS_URL"])


def get_queue():
    from rq import Queue

    return Queue(current_app.config.get("RQ_QUEUE_NAME", "sheetnorm"), connection=get_redis_connection())


def enqueue_conversion(job_id: str, *, rule_id: str | None = None, instruction: str | None = None, generated_rule: dict[str, Any] | None = None) -> dict[str, Any]:
    repository = JobsRepository()
    if current_app.config.get("ASYNC_MODE") == "rq":
        queue_job = get_queue().enqueue(
            "app.workers.queue.run_conversion_job",
            job_id,
            rule_id=rule_id,
            instruction=instruction,
            generated_rule=generated_rule,
            job_timeout=current_app.config.get("SCRIPT_TIMEOUT_SECONDS", 30) + 60,
        )
        return repository.update_status(job_id, "queued", queue_job_id=queue_job.id, progress=0) or {}

    repository.update_status(job_id, "processing", progress=10)
    result = run_conversion_job(job_id, rule_id=rule_id, instruction=instruction, generated_rule=generated_rule)
    status = "success" if "error" not in result else "failed"
    return repository.get(job_id) or {"job_id": job_id, "status": status}


def run_conversion_job(job_id: str, *, rule_id: str | None = None, instruction: str | None = None, generated_rule: dict[str, Any] | None = None) -> dict[str, Any]:
    service = ConversionService()
    if instruction:
        return service.convert_with_instruction_checked(job_id, instruction, generated_rule or {})
    return service.convert_with_rule(job_id, rule_id, options={})

