from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.db.models import (
    ProcessingJob,
    TemplateVersion,
    TransformationTemplate,
    TrainingExample,
    Workspace,
)
from app.extensions import db


def import_legacy_json(
    *,
    rules_file: str | Path,
    jobs_file: str | Path,
    training_examples_file: str | Path,
    workspace_slug: str = "default",
    dry_run: bool = False,
) -> dict[str, Any]:
    workspace = Workspace.query.filter_by(slug=workspace_slug).one_or_none()
    if workspace is None:
        workspace = Workspace(name="Default workspace", slug=workspace_slug, settings_json={})
        db.session.add(workspace)
        db.session.flush()

    summary = {"rules": 0, "jobs": 0, "training_examples": 0, "skipped": 0, "errors": []}

    for rule in _read_json_list(rules_file, summary):
        if not isinstance(rule, dict):
            summary["skipped"] += 1
            continue
        template_id = str(rule.get("id") or "")
        if template_id and db.session.get(TransformationTemplate, template_id):
            summary["skipped"] += 1
            continue
        template = TransformationTemplate(
            id=template_id or None,
            workspace_id=workspace.id,
            name=str(rule.get("name") or "Legacy rule"),
            description=str(rule.get("description") or ""),
            execution_mode="declarative_rule",
            status="active",
        )
        db.session.add(template)
        db.session.flush()
        version = TemplateVersion(
            template_id=template.id,
            version_number=1,
            source_instruction=str(rule.get("raw_user_prompt") or rule.get("prompt") or ""),
            improved_instruction=str(rule.get("ai_improved_instruction") or rule.get("prompt") or ""),
            execution_mode="declarative_rule",
            rule_json=rule.get("generated_rule") or {},
            validation_status="approved",
            validation_report_json={"legacy_import": True},
            change_summary="Imported from legacy rules.json.",
        )
        db.session.add(version)
        db.session.flush()
        template.current_version_id = version.id
        summary["rules"] += 1

    for job in _read_json_list(jobs_file, summary):
        if not isinstance(job, dict):
            summary["skipped"] += 1
            continue
        job_id = str(job.get("job_id") or job.get("id") or "")
        if not job_id or db.session.get(ProcessingJob, job_id):
            summary["skipped"] += 1
            continue
        db.session.add(
            ProcessingJob(
                id=job_id,
                workspace_id=workspace.id,
                status=job.get("status") or "created",
                progress=100 if job.get("status") == "success" else 0,
                input_filename=job.get("input_filename"),
                input_path=job.get("input_path"),
                output_filename=job.get("output_filename"),
                output_path=job.get("output_path"),
                rule_id=job.get("rule_id"),
                rule_name=job.get("rule_name"),
                original_instruction=job.get("original_instruction"),
                improved_instruction=job.get("improved_instruction"),
            )
        )
        summary["jobs"] += 1

    for example in _read_json_list(training_examples_file, summary):
        if not isinstance(example, dict):
            summary["skipped"] += 1
            continue
        db.session.add(
            TrainingExample(
                workspace_id=workspace.id,
                name=str(example.get("name") or example.get("source_filename") or "Legacy example"),
                instruction=str(example.get("prompt") or example.get("instruction") or ""),
                metadata_json=example,
            )
        )
        summary["training_examples"] += 1

    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()
    return summary


def _read_json_list(path: str | Path, summary: dict[str, Any]) -> list[Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        summary["errors"].append(f"{path}: {exc}")
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    summary["errors"].append(f"{path}: root JSON value is not a list or object")
    return []

