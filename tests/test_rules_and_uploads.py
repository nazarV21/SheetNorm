from __future__ import annotations

import io
from pathlib import Path

from openpyxl import Workbook

from app.utils.rules_store import RulesStore
from app.utils.uploads import sanitize_upload_name
from app.utils.file_manager import FileManager


def workbook_bytes() -> io.BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Код", "Значение"])
    sheet.append(["A-1", 10])
    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


def test_safe_filename_removes_path_components():
    safe = sanitize_upload_name("../../secret/report.xlsx")
    assert safe == "report.xlsx"
    assert "/" not in safe and "\\" not in safe


def test_upload_uses_unique_storage_name(client, app):
    response = client.post(
        "/api/uploads",
        data={"file": (workbook_bytes(), "../../monthly report.xlsx")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["stored_as"].endswith("monthly_report.xlsx")
    assert (Path(app.config["INPUT_DIR"]) / payload["stored_as"]).exists()


def test_rule_validation_is_soft_and_persisted(app):
    with app.app_context():
        rule = RulesStore().add_rule(
            name="Тестовый шаблон",
            prompt="Заголовки на строке 4",
            generated_rule={"table_type": "unknown", "header_rows": [3], "data_start_row": 3},
        )
        assert rule["id"]
        assert len(rule["validation_warnings"]) == 2
        assert RulesStore().get_rule(rule["id"])["name"] == "Тестовый шаблон"


def test_rule_detail_and_individual_export(client, app):
    with app.app_context():
        rule = RulesStore().add_rule(name="Exportable", prompt="Read the first sheet")
    detail = client.get(f"/rules/{rule['id']}")
    assert detail.status_code == 200
    assert "Exportable" in detail.get_data(as_text=True)
    exported = client.get(f"/rules/{rule['id']}/export")
    assert exported.status_code == 200
    assert exported.mimetype == "application/json"


def test_output_names_do_not_overwrite_existing_files(tmp_path: Path):
    manager = FileManager(tmp_path / "input", tmp_path / "output")
    first = manager.prepare_output("job-unique", ".xlsx")
    first.touch()
    second = manager.prepare_output("job-unique", ".xlsx")
    assert second != first
    assert second.stem.endswith("_2")
