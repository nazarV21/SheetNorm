from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.db.legacy_import import import_legacy_json
from app.db.models import TemplateVersion, TransformationTemplate, User, Workspace, WorkspaceMember
from app.db.repositories.jobs import DBJobsRepository
from app.extensions import db
from app.scripts_runtime.runner import ScriptExecutionError, ScriptRunner
from app.scripts_runtime.validator import validate_pandas_script
from app.storage.local import LocalStorageBackend
from app.workers.queue import enqueue_conversion


def test_database_models_constraints_and_passwords(app):
    with app.app_context():
        db.create_all()
        user = User(email="owner@example.com", name="Owner")
        user.set_password("secret-password")
        workspace = Workspace(name="Main", slug="main", settings_json={})
        db.session.add_all([user, workspace])
        db.session.flush()
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin"))
        db.session.commit()

        assert user.check_password("secret-password")
        assert not user.check_password("bad-password")
        assert WorkspaceMember.query.filter_by(role="admin").count() == 1


def test_db_jobs_repository_persists_quality_report(app, tmp_path: Path):
    with app.app_context():
        db.create_all()
        repository = DBJobsRepository()
        repository.create("job-db", input_filename="in.xlsx", input_path=str(tmp_path / "in.xlsx"))
        repository.update_status("job-db", "processing", progress=20)
        repository.update_result(
            "job-db",
            output_filename="out.xlsx",
            output_path=str(tmp_path / "out.xlsx"),
            quality_report={
                "quality_status": "success",
                "rows_input": 2,
                "rows_output": 1,
                "columns_input": 2,
                "columns_output": 2,
                "warnings": [],
                "errors": [],
                "applied_operations": ["drop_empty"],
            },
            duration_seconds=1.25,
        )

        stored = repository.get("job-db")
        assert stored["status"] == "success"
        assert stored["quality_report"]["rows_output"] == 1
        assert repository.events("job-db")


def test_legacy_json_import_creates_templates_and_jobs(app, tmp_path: Path):
    rules_file = tmp_path / "rules.json"
    jobs_file = tmp_path / "jobs.json"
    training_file = tmp_path / "training.json"
    rules_file.write_text(
        json.dumps([{"id": "rule-1", "name": "Legacy", "prompt": "Drop empty rows", "generated_rule": {"table_type": "flat"}}]),
        encoding="utf-8",
    )
    jobs_file.write_text(json.dumps([{"job_id": "job-legacy", "status": "success", "input_filename": "in.xlsx"}]), encoding="utf-8")
    training_file.write_text(json.dumps([{"name": "Example", "instruction": "Normalize"}]), encoding="utf-8")

    with app.app_context():
        db.create_all()
        summary = import_legacy_json(
            rules_file=rules_file,
            jobs_file=jobs_file,
            training_examples_file=training_file,
        )
        assert summary["rules"] == 1
        assert summary["jobs"] == 1
        assert TransformationTemplate.query.count() == 1
        assert TemplateVersion.query.filter_by(validation_status="approved").count() == 1


def test_local_storage_blocks_traversal_and_hashes(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    storage = LocalStorageBackend(tmp_path / "storage")
    stored = storage.put_file(source, prefix="workspace-a", original_name="../unsafe.txt")

    assert stored.size_bytes == len("payload")
    assert len(stored.sha256) == 64
    assert storage.open_path(stored.storage_key).read_text(encoding="utf-8") == "payload"
    with pytest.raises(ValueError):
        storage.open_path("../outside.txt")


def test_script_validator_and_runner(app):
    good = "def transform(df):\n    return df.dropna(how='all')\n"
    bad = "import os\ndef transform(df):\n    return df\n"
    assert validate_pandas_script(good).valid
    assert not validate_pandas_script(bad).valid

    with app.app_context():
        result = ScriptRunner.from_app_config().run(good, pd.DataFrame([{"a": 1}, {"a": None}]))
        assert len(result) == 1
        with pytest.raises(ScriptExecutionError):
            ScriptRunner.from_app_config().run("def transform(df):\n    return [1]\n", pd.DataFrame([{"a": 1}]))


def test_sync_queue_uses_existing_conversion_flow(client, app):
    upload = client.post(
        "/api/uploads",
        data={"file": (__import__("io").BytesIO(b""), "empty.xlsx")},
        content_type="multipart/form-data",
    )
    assert upload.status_code == 400
    with app.app_context():
        app.config["ASYNC_MODE"] = "sync"
        # A missing job is handled by the repository/conversion layer, not Redis.
        job = enqueue_conversion("missing-job")
        assert job == {"job_id": "missing-job", "status": "failed"}
