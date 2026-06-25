from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app


JOB_STATUSES = {"created", "queued", "processing", "success", "failed", "cancelled"}
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobsRepository:
    """JSON-backed ProcessingJob repository for pilot deployments.

    Routes and services depend on this interface rather than JSON details, so a
    PostgreSQL implementation can replace it without changing conversion logic.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or current_app.config["JOBS_FILE"])
        self._use_database = False
        if path is None:
            try:
                self._use_database = current_app.config.get("DATA_STORE_BACKEND") == "database"
            except RuntimeError:
                self._use_database = False
        self._db_repo = None

    def _database(self):
        if self._db_repo is None:
            from app.db.repositories.jobs import DBJobsRepository

            self._db_repo = DBJobsRepository()
        return self._db_repo

    def _load_all(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save_all(self, jobs: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def create(
        self,
        job_id: str,
        *,
        input_filename: str,
        input_path: str | Path,
        rule_id: str | None = None,
        rule_name: str | None = None,
        original_instruction: str | None = None,
    ) -> dict[str, Any]:
        if self._use_database:
            return self._database().create(
                job_id,
                input_filename=input_filename,
                input_path=str(input_path),
                rule_id=rule_id,
                rule_name=rule_name,
                original_instruction=original_instruction,
            )
        with _LOCK:
            jobs = self._load_all()
            existing = next((job for job in jobs if job.get("job_id") == job_id), None)
            if existing:
                return existing
            timestamp = _now()
            job = {
                "job_id": job_id,
                "status": "created",
                "created_at": timestamp,
                "updated_at": timestamp,
                "input_filename": input_filename,
                "input_path": str(input_path),
                "output_filename": None,
                "output_path": None,
                "rule_id": rule_id,
                "rule_name": rule_name,
                "original_instruction": original_instruction,
                "improved_instruction": None,
                "detected_table_type": None,
                "rows_input": None,
                "rows_output": None,
                "columns_input": None,
                "columns_output": None,
                "warnings": [],
                "errors": [],
                "applied_operations": [],
                "quality_report": {},
                "duration_seconds": None,
            }
            jobs.append(job)
            self._save_all(jobs)
            return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        if self._use_database:
            return self._database().get(job_id)
        with _LOCK:
            return next((job for job in self._load_all() if job.get("job_id") == job_id), None)

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if self._use_database:
            return self._database().list(status=status)
        with _LOCK:
            jobs = self._load_all()
        if status:
            jobs = [job for job in jobs if job.get("status") == status]
        return sorted(jobs, key=lambda item: item.get("created_at") or "", reverse=True)

    def _update(self, job_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        with _LOCK:
            jobs = self._load_all()
            for job in jobs:
                if job.get("job_id") == job_id:
                    job.update(changes)
                    job["updated_at"] = _now()
                    self._save_all(jobs)
                    return job
        return None

    def update_status(self, job_id: str, status: str, **changes: Any) -> dict[str, Any] | None:
        if self._use_database:
            return self._database().update_status(job_id, status, **changes)
        if status not in JOB_STATUSES:
            raise ValueError(f"Unsupported ProcessingJob status: {status}")
        return self._update(job_id, {"status": status, **changes})

    def update_result(
        self,
        job_id: str,
        *,
        output_filename: str,
        output_path: str | Path,
        quality_report: dict[str, Any],
        duration_seconds: float | None,
        rule_id: str | None = None,
        rule_name: str | None = None,
        original_instruction: str | None = None,
        improved_instruction: str | None = None,
    ) -> dict[str, Any] | None:
        if self._use_database:
            return self._database().update_result(
                job_id,
                output_filename=output_filename,
                output_path=str(output_path),
                quality_report=quality_report,
                duration_seconds=duration_seconds,
                rule_id=rule_id,
                rule_name=rule_name,
                original_instruction=original_instruction,
                improved_instruction=improved_instruction,
            )
        quality = quality_report or {}
        return self.update_status(
            job_id,
            "success",
            output_filename=output_filename,
            output_path=str(output_path),
            rule_id=rule_id,
            rule_name=rule_name,
            original_instruction=original_instruction,
            improved_instruction=improved_instruction,
            detected_table_type=quality.get("detected_table_type"),
            rows_input=quality.get("rows_input"),
            rows_output=quality.get("rows_output"),
            columns_input=quality.get("columns_input"),
            columns_output=quality.get("columns_output"),
            warnings=list(quality.get("warnings") or []),
            errors=list(quality.get("errors") or []),
            applied_operations=list(quality.get("applied_operations") or []),
            quality_report=quality,
            duration_seconds=round(duration_seconds, 3) if duration_seconds is not None else None,
        )

    def append_error(
        self,
        job_id: str,
        error: str,
        *,
        code: str = "CONVERSION_FAILED",
        details: str | None = None,
    ) -> dict[str, Any] | None:
        if self._use_database:
            return self._database().append_error(job_id, error, code=code, details=details)
        job = self.get(job_id)
        if not job:
            return None
        errors = list(job.get("errors") or [])
        errors.append({"code": code, "error": error, "details": details or ""})
        return self.update_status(job_id, "failed", errors=errors)
