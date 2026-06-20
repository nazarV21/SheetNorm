from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl import Workbook

from app.services.conversion_service import ConversionService
from app.utils.jobs_repository import JobsRepository
from app.utils.rules_store import RulesStore


def test_job_create_persist_and_status_transitions(tmp_path: Path):
    path = tmp_path / "jobs.json"
    repository = JobsRepository(path)
    created = repository.create(
        "job-1",
        input_filename="report.xlsx",
        input_path=tmp_path / "report.xlsx",
    )
    assert created["status"] == "created"

    repository.update_status("job-1", "queued")
    repository.update_status("job-1", "processing", rule_name="Monthly report")
    persisted = JobsRepository(path).get("job-1")
    assert persisted is not None
    assert persisted["status"] == "processing"
    assert persisted["rule_name"] == "Monthly report"

    quality = {
        "rows_input": 3,
        "rows_output": 2,
        "columns_input": 2,
        "columns_output": 2,
        "empty_cells_before": 0,
        "empty_cells_after": 0,
        "detected_table_type": "flat",
        "applied_operations": ["Удаление пустых строк и колонок"],
        "warnings": [],
        "errors": [],
        "quality_status": "success",
    }
    repository.update_result(
        "job-1",
        output_filename="result.xlsx",
        output_path=tmp_path / "result.xlsx",
        quality_report=quality,
        duration_seconds=1.23456,
    )
    completed = JobsRepository(path).get("job-1")
    assert completed["status"] == "success"
    assert completed["duration_seconds"] == 1.235
    assert completed["quality_report"]["quality_status"] == "success"


def test_job_append_error_sets_failed(tmp_path: Path):
    repository = JobsRepository(tmp_path / "jobs.json")
    repository.create("job-2", input_filename="bad.xlsx", input_path=tmp_path / "bad.xlsx")
    repository.append_error("job-2", "Не найден лист", code="SHEET_NOT_FOUND")
    failed = repository.get("job-2")
    assert failed["status"] == "failed"
    assert failed["errors"][0]["code"] == "SHEET_NOT_FOUND"


def test_quality_report_is_mandatory_and_complete(app, tmp_path: Path):
    source = tmp_path / "quality.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Код", "Сумма"])
    sheet.append(["A", 10])
    sheet.append(["B", None])
    workbook.save(source)

    with app.app_context():
        report = ConversionService()._build_quality_report(
            source,
            pd.DataFrame([{"Код": "A", "Сумма": 10}]),
            {"table_type": "flat", "generated_rule": {"table_type": "flat", "header_rows": [0]}},
        )
    required = {
        "rows_input", "rows_output", "columns_input", "columns_output",
        "empty_cells_before", "empty_cells_after", "detected_table_type",
        "applied_operations", "warnings", "quality_status",
    }
    assert required <= report.keys()
    assert report["quality_status"] == "success"


def test_old_rules_json_gets_backward_compatible_defaults(app):
    old_rule = [{"id": "legacy", "name": "Legacy", "prompt": "Read first sheet"}]
    path = Path(app.config["RULES_FILE"])
    path.write_text(__import__("json").dumps(old_rule), encoding="utf-8")
    with app.app_context():
        loaded = RulesStore().list_rules()[0]
    assert loaded["category"] == "universal"
    assert loaded["table_type"] == "flat"
    assert loaded["version"] == 1
