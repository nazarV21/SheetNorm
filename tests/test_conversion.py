from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.conversion_service import ConversionService, ConversionValidationError
from app.services.table_structure_analyzer import TableStructureAnalyzer


def make_workbook(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Данные"
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def test_analyzer_detects_simple_table(tmp_path: Path):
    source = tmp_path / "simple.xlsx"
    make_workbook(source, [["Код", "Наименование", "Сумма"], [1, "A", 10], [2, "B", 20]])
    analysis = TableStructureAnalyzer().analyze_excel(source)
    assert analysis["selected_sheet"] == "Данные"
    assert analysis["header_rows_human"] == [1]
    assert analysis["data_start_row_human"] == 2


def test_cross_table_is_converted_to_long_format(app, tmp_path: Path):
    source = tmp_path / "cross.xlsx"
    make_workbook(source, [["Товар", "Январь", "Февраль"], ["A", 10, 12], ["B", 20, 24]])
    rule = {
        "sheet_name": "Данные",
        "table_type": "cross_table",
        "header_rows": [0],
        "data_start_row": 1,
        "id_columns": [{"name": "Товар"}],
        "value_columns": [{"name": "Январь"}, {"name": "Февраль"}],
        "melt": {"enabled": True, "var_name": "Период", "value_name": "Значение"},
    }
    with app.app_context():
        result = ConversionService()._apply_generated_rule(source, rule)
    assert list(result.columns) == ["Товар", "Период", "Значение"]
    assert len(result) == 4


def test_missing_sheet_has_structured_error_code(app, tmp_path: Path):
    source = tmp_path / "sheet.xlsx"
    make_workbook(source, [["Код"], ["A"]])
    with app.app_context():
        service = ConversionService()
        with pytest.raises(ConversionValidationError) as caught:
            service._read_raw(source, "Несуществующий лист")
        assert service._validation_error_code(caught.value) == "SHEET_NOT_FOUND"
