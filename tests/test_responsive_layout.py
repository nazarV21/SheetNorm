from pathlib import Path


def test_layout_avoids_horizontal_scroll_patterns():
    root = Path(__file__).resolve().parents[1]
    layout = (root / "templates" / "layout.html").read_text(encoding="utf-8")
    jobs = (root / "templates" / "jobs.html").read_text(encoding="utf-8")
    assert "overflow-x: auto" not in layout
    assert "width: max-content" not in layout
    assert "min-width:1080px" not in jobs
    assert "workbook-column-toolbar" in layout
    assert "data-column-index" in layout
    assert "responsive-record-list jobs-list" in jobs


def test_workbook_partial_supports_column_paging():
    root = Path(__file__).resolve().parents[1]
    partial = (root / "templates" / "_workbook_preview.html").read_text(encoding="utf-8")
    assert "data-column-count" in partial
    assert "data-column-index" in partial
