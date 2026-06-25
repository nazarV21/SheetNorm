from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from app.utils.jobs_repository import JobsRepository


def _write_book(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Данные"
    sheet["A1"] = value
    workbook.save(path)
    workbook.close()


def test_processing_job_does_not_render_assistant_preview_as_final_result(app, client, tmp_path: Path):
    source = tmp_path / "input" / "source.xlsx"
    _write_book(source, "SOURCE_MARKER")

    with app.app_context():
        repository = JobsRepository()
        repository.create(
            "job-processing",
            input_filename="source.xlsx",
            input_path=source,
            job_kind="assistant",
            original_instruction="Разделить по складам",
            assistant_state={
                "target_preview": {
                    "filename": "draft.xlsx",
                    "sheets": [
                        {
                            "name": "Черновик",
                            "rows": [["SHOULD_NOT_RENDER_AS_FINAL"]],
                            "max_columns": 1,
                        }
                    ],
                }
            },
        )
        repository.update_status("job-processing", "processing", stage="executing_transformation", progress=45)

    response = client.get("/jobs/job-processing")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Таблица ещё обрабатывается" in html
    assert "SHOULD_NOT_RENDER_AS_FINAL" not in html
    assert "SOURCE_MARKER" in html


def test_success_job_renders_the_actual_downloadable_output(app, client, tmp_path: Path):
    source = tmp_path / "input" / "source.xlsx"
    output = tmp_path / "output" / "source_converted.xlsx"
    _write_book(source, "SOURCE_ONLY")
    _write_book(output, "FINAL_RESULT_MARKER")

    with app.app_context():
        repository = JobsRepository()
        repository.create(
            "job-success",
            input_filename="source.xlsx",
            input_path=source,
            original_instruction="Оставить только итоговые данные",
        )
        repository.update_result(
            "job-success",
            output_filename=output.name,
            output_path=output,
            quality_report={"rows_input": 1, "rows_output": 1, "warnings": [], "errors": []},
            duration_seconds=0.1,
            original_instruction="Оставить только итоговые данные",
            improved_instruction="Оставить только итоговые данные",
        )

    response = client.get("/jobs/job-success")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "FINAL_RESULT_MARKER" in html
    assert "Это итоговая таблица из того же файла" in html
    assert "Скачать результат" in html


def test_edit_completed_job_creates_new_assistant_draft_without_overwriting_history(app, client, tmp_path: Path):
    source = tmp_path / "input" / "source.xlsx"
    output = tmp_path / "output" / "source_converted.xlsx"
    _write_book(source, "SOURCE")
    _write_book(output, "RESULT")

    with app.app_context():
        repository = JobsRepository()
        repository.create(
            "historical-job",
            input_filename="source.xlsx",
            input_path=source,
            original_instruction="Старый промпт",
        )
        repository.update_result(
            "historical-job",
            output_filename=output.name,
            output_path=output,
            quality_report={"warnings": [], "errors": []},
            duration_seconds=0.1,
            original_instruction="Старый промпт",
            improved_instruction="Уточнённый старый промпт",
        )

    response = client.post("/jobs/historical-job/edit", follow_redirects=False)
    assert response.status_code == 302
    assert "/assistant?job_id=" in response.headers["Location"]
    new_job_id = response.headers["Location"].split("job_id=", 1)[1]

    with app.app_context():
        repository = JobsRepository()
        old_job = repository.get("historical-job")
        new_job = repository.get(new_job_id)

    assert old_job["status"] == "success"
    assert old_job["output_filename"] == output.name
    assert new_job is not None
    assert new_job["status"] == "created"
    assert new_job["job_kind"] == "assistant"
    assert new_job["original_instruction"] == "Старый промпт"
    assert Path(new_job["input_path"]).exists()
