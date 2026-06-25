from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.db.models import TemplateVersion, TransformationTemplate, model_to_dict
from app.extensions import db


class TemplateRepository:
    def create_template(
        self,
        *,
        workspace_id: str | None,
        name: str,
        description: str = "",
        execution_mode: str = "declarative_rule",
        created_by_id: str | None = None,
    ) -> dict[str, Any]:
        template = TransformationTemplate(
            workspace_id=workspace_id,
            name=name,
            description=description,
            execution_mode=execution_mode,
            created_by_id=created_by_id,
        )
        db.session.add(template)
        db.session.commit()
        return model_to_dict(template)

    def add_version(
        self,
        template_id: str,
        *,
        source_instruction: str = "",
        improved_instruction: str = "",
        execution_mode: str = "declarative_rule",
        rule_json: dict[str, Any] | None = None,
        script_code: str | None = None,
        script_explanation: str | None = None,
        created_by_id: str | None = None,
        change_summary: str = "",
    ) -> dict[str, Any] | None:
        template = db.session.get(TransformationTemplate, template_id)
        if not template:
            return None
        latest = (
            TemplateVersion.query.filter_by(template_id=template_id)
            .order_by(TemplateVersion.version_number.desc())
            .first()
        )
        checksum = hashlib.sha256(script_code.encode("utf-8")).hexdigest() if script_code else None
        version = TemplateVersion(
            template_id=template_id,
            version_number=(latest.version_number + 1) if latest else 1,
            source_instruction=source_instruction,
            improved_instruction=improved_instruction or source_instruction,
            execution_mode=execution_mode,
            rule_json=rule_json,
            script_code=script_code,
            script_explanation=script_explanation,
            script_checksum=checksum,
            created_by_id=created_by_id,
            change_summary=change_summary,
        )
        db.session.add(version)
        db.session.commit()
        return model_to_dict(version)

    def validate_version(self, version_id: str, report: dict[str, Any]) -> dict[str, Any] | None:
        version = db.session.get(TemplateVersion, version_id)
        if not version:
            return None
        version.validation_report_json = report
        version.validation_status = "valid" if report.get("valid") else "invalid"
        db.session.commit()
        return model_to_dict(version)

    def approve_version(self, version_id: str, approved_by_id: str | None = None) -> dict[str, Any] | None:
        version = db.session.get(TemplateVersion, version_id)
        if not version:
            return None
        if version.validation_status not in {"valid", "approved"}:
            raise ValueError("Only valid versions can be approved.")
        version.validation_status = "approved"
        version.approved_by_id = approved_by_id
        version.approved_at = datetime.now(timezone.utc)
        template = db.session.get(TransformationTemplate, version.template_id)
        if template:
            template.current_version_id = version.id
            template.status = "active"
        db.session.commit()
        return model_to_dict(version)

    def reject_version(self, version_id: str, report: dict[str, Any] | None = None) -> dict[str, Any] | None:
        version = db.session.get(TemplateVersion, version_id)
        if not version:
            return None
        version.validation_status = "rejected"
        version.validation_report_json = report or version.validation_report_json or {}
        db.session.commit()
        return model_to_dict(version)

    def list_templates(self, *, workspace_id: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        query = TransformationTemplate.query
        if workspace_id:
            query = query.filter_by(workspace_id=workspace_id)
        return [model_to_dict(row) for row in query.order_by(TransformationTemplate.created_at.desc()).limit(limit).offset(offset).all()]

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        row = db.session.get(TransformationTemplate, template_id)
        return model_to_dict(row) if row else None

