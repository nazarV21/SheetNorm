from __future__ import annotations

import io

from openpyxl import Workbook


def simple_workbook() -> io.BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Данные"
    sheet.append(["Код", "Сумма"])
    sheet.append(["A-1", 10])
    sheet.append(["A-2", 20])
    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"service": "SheetNorm", "status": "ok"}


def test_missing_job_returns_structured_error(client):
    response = client.post("/api/convert/missing-job", json={})
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["code"] == "JOB_NOT_FOUND"
    assert payload["error"]
    assert payload["suggestion"]


def test_upload_rejects_non_excel(client):
    response = client.post(
        "/api/uploads",
        data={"file": (io.BytesIO(b"not excel"), "report.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "UNSUPPORTED_FILE_TYPE"


def test_main_pages_render(client):
    for path in ("/", "/convert", "/assistant", "/rules", "/training", "/jobs", "/history", "/about", "/deployment", "/settings"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "SheetNorm" in response.get_data(as_text=True), path


def test_assistant_fallback_builds_preview(client):
    response = client.post(
        "/assistant",
        data={
            "file": (simple_workbook(), "simple.xlsx"),
            "raw_prompt": "Заголовки на строке 1, данные начинаются со строки 2.",
        },
        content_type="multipart/form-data",
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Предпросмотр итоговой таблицы" in page
    assert "Код" in page and "Сумма" in page


def test_job_not_found_api(client):
    response = client.get("/api/jobs/unknown-job")
    assert response.status_code == 404
    assert response.get_json()["code"] == "JOB_NOT_FOUND"
    page = client.get("/jobs/unknown-job")
    assert page.status_code == 404
    assert "Задача не найдена" in page.get_data(as_text=True)


def test_download_blocks_path_traversal(client):
    response = client.get("/download/%2e%2e%2fREADME.md")
    assert response.status_code == 404


def test_upload_and_conversion_persist_successful_job(client):
    upload = client.post(
        "/api/uploads",
        data={"file": (simple_workbook(), "pipeline.xlsx")},
        content_type="multipart/form-data",
    )
    job_id = upload.get_json()["job_id"]
    conversion = client.post(f"/api/convert/{job_id}", json={})
    assert conversion.status_code == 202
    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["status"] == "success"
    assert job["quality_report"]["quality_status"] == "success"
    assert job["output_filename"].endswith(".xlsx")
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "Отчёт качества" in detail.get_data(as_text=True)


def test_assistant_api_reports_fallback_mode(client):
    response = client.post(
        "/api/assistant/analyze",
        data={"file": (simple_workbook(), "assistant.xlsx"), "raw_prompt": "Прочитай таблицу"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    warnings = response.get_json()["warnings"]
    assert warnings[0]["code"] == "LLM_UNAVAILABLE_FALLBACK_USED"
