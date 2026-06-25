from __future__ import annotations

import io
import os
import subprocess
import sys

from openpyxl import Workbook
from sqlalchemy import text

from app import create_app
from app.extensions import db
from config import Config


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


def test_local_sqlite_database_is_initialized_for_dev(tmp_path):
    local_config = type(
        "LocalSQLiteConfig",
        (Config,),
        {
            "TESTING": True,
            "DATA_STORE_BACKEND": "database",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'sheetnorm.db'}",
            "AUTO_CREATE_SQLITE_DB": True,
            "INPUT_DIR": tmp_path / "input",
            "OUTPUT_DIR": tmp_path / "output",
            "HISTORY_FILE": tmp_path / "history.json",
            "JOBS_FILE": tmp_path / "jobs.json",
            "RULES_FILE": tmp_path / "rules.json",
            "TRAINING_EXAMPLES_FILE": tmp_path / "training_examples.json",
            "TRAINING_EXAMPLES_DIR": tmp_path / "training_examples",
            "STORAGE_ROOT": tmp_path / "storage",
        },
    )
    application = create_app(local_config)
    client = application.test_client()

    with application.app_context():
        assert db.session.execute(text("select count(*) from processing_jobs")).scalar() == 0

    assert client.get("/").status_code == 200
    assert client.get("/jobs").status_code == 200


def test_main_loads_dotenv_before_app_creation(tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("AI_BACKEND=dotenv_llm\nAI_MODEL_PATH=models/test-model.gguf\nDATA_STORE_BACKEND=json\n")
    env = os.environ.copy()
    env.pop("AI_BACKEND", None)
    env.pop("AI_MODEL_PATH", None)
    env["SHEETNORM_DOTENV_PATH"] = str(dotenv_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import main; print(main.app.config['AI_BACKEND']); print(main.app.config['AI_MODEL_PATH'])",
        ],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.splitlines() == ["dotenv_llm", "models/test-model.gguf"]


def test_llm_prompt_is_compacted_to_context_window(app):
    from app.services.ai.instruction_assistant import InstructionAssistant

    class FakeLLM:
        def tokenize(self, prompt: bytes, add_bos: bool = True):
            return list(range(max(1, len(prompt) // 3 + int(add_bos))))

    assistant = object.__new__(InstructionAssistant)
    assistant.context_tokens = 2048
    assistant._llm = FakeLLM()

    with app.app_context():
        prompt, max_tokens = assistant._fit_prompt_to_context("очень длинный контекст\n" * 2000, 1000)

    assert assistant._count_tokens(prompt) + max_tokens + 128 <= assistant.context_tokens
    assert max_tokens >= 128


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


def test_assistant_without_prompt_only_analyzes_source(client, monkeypatch):
    def fail_preview(*_args, **_kwargs):
        raise AssertionError("assistant must not build result preview without a user instruction")

    monkeypatch.setattr("app.routes.web._preview_for_assistant", fail_preview)
    response = client.post(
        "/assistant",
        data={"file": (simple_workbook(), "source-only.xlsx"), "raw_prompt": ""},
        content_type="multipart/form-data",
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Итоговый предпросмотр ещё не строился" in page
    assert "Возможные уточнения" in page


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
