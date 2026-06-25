from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.models import JobEvent, ProcessingJob, QualityReport, model_to_dict
from app.extensions import db
from app.utils.jobs_repository import JOB_STATUSES


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DBJobsRepository:
    def create(
        self,
        job_id: str,
        *,
        input_filename: str,
        input_path: str,
        rule_id: str | None = None,
        rule_name: str | None = None,
        original_instruction: str | None = None,
        workspace_id: str | None = None,
        template_id: str | None = None,
        template_version_id: str | None = None,
    ) -> dict[str, Any]:
        existing = db.session.get(ProcessingJob, job_id)
        if existing:
            return self._serialize(existing)
        job = ProcessingJob(
            id=job_id,
            workspace_id=workspace_id,
            template_id=template_id,
            template_version_id=template_version_id,
            input_filename=input_filename,
            input_path=str(input_path),
            rule_id=rule_id,
            rule_name=rule_name,
            original_instruction=original_instruction,
        )
        db.session.add(job)
        db.session.add(
            JobEvent(
                job=job,
                event_type="created",
                stage="created",
                message="Processing job created.",
                payload_json={"input_filename": input_filename},
            )
        )
        db.session.commit()
        return self._serialize(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = db.session.get(ProcessingJob, job_id)
        return self._serialize(job) if job else None

    def list(self, *, status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        query = ProcessingJob.query
        if status:
            query = query.filter_by(status=status)
        rows = query.order_by(ProcessingJob.created_at.desc()).limit(limit).offset(offset).all()
        return [self._serialize(job) for job in rows]

    def update_status(self, job_id: str, status: str, **changes: Any) -> dict[str, Any] | None:
        if status not in JOB_STATUSES and status != "requires_review":
            raise ValueError(f"Unsupported ProcessingJob status: {status}")
        job = db.session.get(ProcessingJob, job_id)
        if not job:
            return None
        job.status = status
        job.stage = changes.pop("stage", status)
        job.progress = int(changes.pop("progress", job.progress or 0))
        if status == "queued" and not job.queued_at:
            job.queued_at = _utc_now()
        if status == "processing" and not job.started_at:
            job.started_at = _utc_now()
        if status in {"success", "failed", "cancelled"}:
            job.finished_at = _utc_now()
        for key, value in changes.items():
            if hasattr(job, key):
                setattr(job, key, value)
        db.session.add(
            JobEvent(
                job_id=job.id,
                event_type=status,
                stage=job.stage,
                message=f"Job status changed to {status}.",
                payload_json=changes,
            )
        )
        db.session.commit()
        return self._serialize(job)

    def update_result(
        self,
        job_id: str,
        *,
        output_filename: str,
        output_path: str,
        quality_report: dict[str, Any],
        duration_seconds: float | None,
        rule_id: str | None = None,
        rule_name: str | None = None,
        original_instruction: str | None = None,
        improved_instruction: str | None = None,
    ) -> dict[str, Any] | None:
        job = db.session.get(ProcessingJob, job_id)
        if not job:
            return None
        report = QualityReport.query.filter_by(job_id=job_id).one_or_none()
        if report is None:
            report = QualityReport(job_id=job_id)
        warnings = list(quality_report.get("warnings") or [])
        errors = list(quality_report.get("errors") or [])
        report.quality_status = quality_report.get("quality_status") or "success"
        report.rows_input = quality_report.get("rows_input")
        report.rows_output = quality_report.get("rows_output")
        report.columns_input = quality_report.get("columns_input")
        report.columns_output = quality_report.get("columns_output")
        report.empty_cells_before = quality_report.get("empty_cells_before")
        report.empty_cells_after = quality_report.get("empty_cells_after")
        report.warnings_count = len(warnings)
        report.errors_count = len(errors)
        report.confidence_score = quality_report.get("confidence_score")
        report.detected_table_type = quality_report.get("detected_table_type")
        report.applied_operations_json = list(quality_report.get("applied_operations") or [])
        report.warnings_json = warnings
        report.metrics_json = dict(quality_report)
        db.session.add(report)
        db.session.flush()
        duration_ms = int(duration_seconds * 1000) if duration_seconds is not None else None
        return self.update_status(
            job_id,
            "success",
            progress=100,
            output_filename=output_filename,
            output_path=str(output_path),
            quality_report_id=report.id,
            duration_ms=duration_ms,
            rule_id=rule_id,
            rule_name=rule_name,
            original_instruction=original_instruction,
            improved_instruction=improved_instruction,
        )

    def append_error(
        self,
        job_id: str,
        error: str,
        *,
        code: str = "CONVERSION_FAILED",
        details: str | None = None,
    ) -> dict[str, Any] | None:
        job = db.session.get(ProcessingJob, job_id)
        if not job:
            return None
        return self.update_status(
            job_id,
            "failed",
            progress=job.progress or 0,
            error_code=code,
            error_message=error,
            error_details={"details": details or ""},
        )

    def events(self, job_id: str) -> list[dict[str, Any]]:
        return [
            model_to_dict(event)
            for event in JobEvent.query.filter_by(job_id=job_id).order_by(JobEvent.created_at.asc()).all()
        ]

    def _serialize(self, job: ProcessingJob) -> dict[str, Any]:
        data = model_to_dict(job)
        data["job_id"] = data["id"]
        if job.quality_report_id:
            report = db.session.get(QualityReport, job.quality_report_id)
            data["quality_report"] = report.metrics_json if report else {}
        else:
            data["quality_report"] = {}
        data["duration_seconds"] = round(job.duration_ms / 1000, 3) if job.duration_ms is not None else None
        data["warnings"] = (data["quality_report"] or {}).get("warnings", [])
        data["errors"] = (
            [{"code": job.error_code, "error": job.error_message, "details": job.error_details}]
            if job.error_code
            else (data["quality_report"] or {}).get("errors", [])
        )
        return data

