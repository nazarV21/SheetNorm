from __future__ import annotations

import io
import threading
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook

from app.services.conversion_service import ConversionCancelledError, ConversionService
from app.utils.jobs_repository import JobsRepository
from app.workers.queue import cancel_conversion, enqueue_conversion


def workbook_stream() -> io.BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Код", "Сумма"])
    sheet.append(["A", 10])
    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


def test_model_cards_use_block_recommendation_layout(client):
    response = client.get("/settings")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert ".model-option > span { display:block; min-width:0; }" in page
    assert ".recommendation { display:block; width:100%;" in page


def test_convert_page_enqueues_and_redirects_to_job(client, monkeypatch):
    captured: dict = {}

    def fake_enqueue(job_id: str, **kwargs):
        captured["job_id"] = job_id
        captured.update(kwargs)
        return {"job_id": job_id, "status": "queued"}

    monkeypatch.setattr("app.routes.web.enqueue_conversion", fake_enqueue)
    response = client.post(
        "/convert",
        data={
            "file": (workbook_stream(), "report.xlsx"),
            "mode": "rule",
            "rule_id": "",
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert f"/jobs/{captured['job_id']}" in response.headers["Location"]


def test_local_background_task_survives_request_and_can_be_cancelled(app, monkeypatch, tmp_path: Path):
    started = threading.Event()

    def fake_conversion(job_id: str, *, cancel_event=None, **_kwargs):
        repository = JobsRepository()
        repository.update_status(job_id, "processing", stage="test_wait", progress=40)
        started.set()
        assert cancel_event is not None
        cancel_event.wait(timeout=3)
        if cancel_event.is_set():
            repository.update_status(job_id, "cancelled", stage="cancelled", progress=40)
            return {"job_id": job_id, "status": "cancelled"}
        repository.update_status(job_id, "success", stage="completed", progress=100)
        return {"job_id": job_id, "status": "success"}

    monkeypatch.setattr("app.workers.queue.run_conversion_job", fake_conversion)

    source = tmp_path / "source.xlsx"
    source.write_bytes(b"placeholder")
    with app.app_context():
        app.config["ASYNC_MODE"] = "thread"
        repository = JobsRepository()
        repository.create("background-1", input_filename="source.xlsx", input_path=source)
        queued = enqueue_conversion("background-1")
        assert queued["status"] in {"queued", "processing"}

    assert started.wait(timeout=2)

    with app.app_context():
        cancelled = cancel_conversion("background-1")
        assert cancelled is not None
        assert cancelled["status"] == "cancelled"

    with app.app_context():
        final = JobsRepository().get("background-1")
        assert final is not None
        assert final["status"] == "cancelled"


def test_cancelled_job_cannot_save_success_result(app, tmp_path: Path):
    source = Path(app.config["INPUT_DIR"]) / "cancelled.xlsx"
    source.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.active.append(["a"])
    workbook.active.append([1])
    workbook.save(source)

    with app.app_context():
        repository = JobsRepository()
        repository.create("cancelled-job", input_filename="cancelled.xlsx", input_path=source)
        repository.update_status("cancelled-job", "cancelled", stage="cancelled", progress=50)
        service = ConversionService()
        with pytest.raises(ConversionCancelledError):
            service._finish(
                "cancelled-job",
                source,
                pd.DataFrame({"a": [1]}),
                None,
            )
        assert repository.get("cancelled-job")["status"] == "cancelled"
        assert not list(Path(app.config["OUTPUT_DIR"]).glob("*cancelled-job*"))


def test_jobs_pages_render_active_controls(client, app, tmp_path: Path):
    source = tmp_path / "active.xlsx"
    source.write_bytes(b"placeholder")
    with app.app_context():
        JobsRepository().create("active-job", input_filename="active.xlsx", input_path=source)
        JobsRepository().update_status(
            "active-job",
            "processing",
            stage="executing_transformation",
            progress=35,
        )

    list_response = client.get("/jobs")
    detail_response = client.get("/jobs/active-job")
    assert list_response.status_code == 200
    assert "Остановить" in list_response.get_data(as_text=True)
    assert detail_response.status_code == 200
    detail_page = detail_response.get_data(as_text=True)
    assert "Обработка выполняется в фоне" in detail_page
    assert "job-progress-fill" in detail_page


def test_real_thread_conversion_continues_after_navigation(client, app):
    import time

    with app.app_context():
        app.config["ASYNC_MODE"] = "thread"

    response = client.post(
        "/convert",
        data={
            "file": (workbook_stream(), "background-real.xlsx"),
            "mode": "rule",
            "rule_id": "",
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302
    job_id = response.headers["Location"].rstrip("/").split("/")[-1]

    # Opening another page must not cancel the server-side conversion.
    assert client.get("/settings").status_code == 200

    deadline = time.time() + 5
    final = None
    while time.time() < deadline:
        with app.app_context():
            final = JobsRepository().get(job_id)
        if final and final.get("status") in {"success", "failed", "cancelled"}:
            break
        time.sleep(0.05)

    assert final is not None
    assert final["status"] == "success"
    assert final["output_filename"].endswith(".xlsx")
