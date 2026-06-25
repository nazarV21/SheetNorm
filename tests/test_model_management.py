from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

from app.services.ai.hardware_info import HardwareInfo
from app.services.ai.model_manager import get_model_manager
from app.services.ai.model_recommendation import ModelRecommendationService
from app.services.ai.model_registry import ModelRegistry
from app.services.ai.settings_service import AISettingsService


def _fake_gguf(path: Path, size: int = 2048) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + b"\x00" * max(0, size - 4))
    return path


def test_model_registry_scans_nested_models_and_sidecar(tmp_path: Path):
    models_dir = tmp_path / "models"
    model_path = _fake_gguf(models_dir / "qwen" / "qwen2.5-coder-7b-instruct-q4_k_m.gguf")
    model_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "display_name": "Qwen Coder 7B",
                "family": "Qwen 2.5",
                "parameters_b": 7,
                "quantization": "Q4_K_M",
                "minimum_ram_gb": 8,
            }
        ),
        encoding="utf-8",
    )
    (models_dir / "ignore.txt").write_text("not a model", encoding="utf-8")

    registry = ModelRegistry(models_dir)
    models = registry.scan_models()

    assert len(models) == 1
    model = models[0]
    assert model.display_name == "Qwen Coder 7B"
    assert model.relative_path == "qwen/qwen2.5-coder-7b-instruct-q4_k_m.gguf"
    assert model.parameter_hint == 7
    assert model.quantization_hint == "Q4_K_M"
    assert registry.get_model(model.id).relative_path == model.relative_path
    assert registry.resolve_path(model.relative_path) == model_path.resolve()


def test_model_registry_does_not_escape_root(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    outside = _fake_gguf(tmp_path / "outside.gguf")
    link = models_dir / "linked.gguf"
    try:
        link.symlink_to(outside)
    except OSError:
        return

    assert ModelRegistry(models_dir).scan_models() == []


def test_recommendation_marks_too_large_model_as_heavy(tmp_path: Path):
    model_path = _fake_gguf(tmp_path / "huge-70b-q4_k_m.gguf")
    model = ModelRegistry(tmp_path).scan_models()[0]
    object.__setattr__(model, "size_gb", 20.0)
    hardware = HardwareInfo(
        operating_system="Test",
        architecture="x86_64",
        logical_cpu_count=8,
        physical_cpu_count=4,
        total_ram_gb=8,
        available_ram_gb=6,
        container_memory_limit_gb=None,
        gpu_backend="CPU",
        gpu_name=None,
        gpu_vram_gb=None,
    )

    recommendation = ModelRecommendationService().evaluate(model, hardware)
    assert recommendation.status == "heavy"
    assert recommendation.recommended_profile == "economy"


def test_ai_settings_file_persistence(app, tmp_path: Path):
    with app.app_context():
        app.config.update(
            DATA_STORE_BACKEND="json",
            AI_SETTINGS_FILE=tmp_path / "ai_settings.json",
            AI_MODELS_DIR=tmp_path / "models",
            AI_MODEL_PATH="",
        )
        service = AISettingsService()
        saved = service.save(
            {
                "backend": "fallback",
                "selected_model_relative_path": "qwen/model.gguf",
                "performance_profile": "custom",
                "context_tokens": 3072,
                "max_completion_tokens": 800,
                "n_threads": 3,
                "n_batch": 96,
                "n_gpu_layers": 0,
                "temperature": 0.2,
            }
        )
        loaded = service.get()

    assert saved.selected_model_relative_path == "qwen/model.gguf"
    assert loaded.context_tokens == 3072
    assert loaded.n_threads == 3


def test_settings_page_lists_models(client, app, tmp_path: Path):
    models_dir = tmp_path / "models"
    _fake_gguf(models_dir / "qwen2.5-coder-3b-instruct-q4_k_m.gguf")
    with app.app_context():
        app.config.update(
            AI_MODELS_DIR=models_dir,
            AI_SETTINGS_FILE=tmp_path / "ai_settings.json",
            AI_MODEL_PATH="",
        )

    response = client.get("/settings")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "qwen2.5 coder 3b instruct q4 k m" in page.lower()
    assert "Протестировать и активировать" in page


def test_model_manager_tests_activates_and_reuses_model(app, tmp_path: Path, monkeypatch):
    models_dir = tmp_path / "models"
    _fake_gguf(models_dir / "tiny-3b-q4.gguf")
    created: list[dict] = []

    class FakeLlama:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.closed = False

        def create_completion(self, **_kwargs):
            return {"choices": [{"text": "OK"}]}

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=FakeLlama))

    with app.app_context():
        app.config.update(
            DATA_STORE_BACKEND="json",
            AI_MODELS_DIR=models_dir,
            AI_SETTINGS_FILE=tmp_path / "ai_settings.json",
            AI_MODEL_PATH="",
        )
        model = ModelRegistry(models_dir).scan_models()[0]
        AISettingsService().save(
            {
                "selected_model_relative_path": model.relative_path,
                "performance_profile": "balanced",
                "context_tokens": 4096,
                "max_completion_tokens": 500,
                "n_threads": 2,
                "n_batch": 64,
                "n_gpu_layers": 0,
                "temperature": 0.1,
            }
        )
        manager = get_model_manager()
        result = manager.test_selected_model()
        settings = manager.activate_selected_model()
        first = manager.get_active_model()
        second = manager.get_active_model()

    assert result["status"] == "success"
    assert settings.backend == "llama_cpp"
    assert first is second
    assert created[-1]["n_threads"] == 2
    assert created[-1]["n_ctx"] == 4096


def test_ai_settings_api_lists_models(client, app, tmp_path: Path):
    models_dir = tmp_path / "models"
    _fake_gguf(models_dir / "phi-3-mini-4b-q4.gguf")
    with app.app_context():
        app.config.update(
            AI_MODELS_DIR=models_dir,
            AI_SETTINGS_FILE=tmp_path / "ai_settings.json",
            AI_MODEL_PATH="",
        )

    response = client.get("/api/settings/ai/models")
    payload = response.get_json()

    assert response.status_code == 200
    assert len(payload["items"]) == 1
    assert payload["items"][0]["relative_path"] == "phi-3-mini-4b-q4.gguf"


def test_ai_clients_do_not_eagerly_load_model(app, monkeypatch):
    from app.services.ai.instruction_assistant import InstructionAssistant
    from app.services.ai.llm_client import AIClient
    from app.services.ai.settings_service import RuntimeAISettings

    class FakeManager:
        def get_runtime_settings(self):
            return RuntimeAISettings(
                backend="llama_cpp",
                active_model_relative_path="model.gguf",
            )

        def get_active_model(self):
            raise AssertionError("GGUF must not load while constructing AI services")

    fake = FakeManager()
    monkeypatch.setattr("app.services.ai.llm_client.get_model_manager", lambda: fake)
    monkeypatch.setattr("app.services.ai.instruction_assistant.get_model_manager", lambda: fake)

    with app.app_context():
        client = AIClient()
        assistant = InstructionAssistant()

    assert client.backend == "llama_cpp"
    assert client._llm is None
    assert assistant.backend == "llama_cpp"
    assert assistant._llm is None


def test_ai_memory_settings_are_persisted(app, tmp_path: Path):
    with app.app_context():
        app.config.update(
            DATA_STORE_BACKEND="json",
            AI_SETTINGS_FILE=tmp_path / "ai_settings.json",
            AI_MODELS_DIR=tmp_path / "models",
            AI_MODEL_PATH="",
        )
        saved = AISettingsService().save(
            {
                "memory_mode": "economy",
                "idle_unload_seconds": 180,
            }
        )
        loaded = AISettingsService().get()

    assert saved.memory_mode == "economy"
    assert loaded.memory_mode == "economy"
    assert loaded.idle_unload_seconds == 180


def _model_info(name: str, *, size_gb: float, parameters_b: float):
    from app.services.ai.model_registry import LocalModelInfo

    return LocalModelInfo(
        id=name,
        display_name=name,
        filename=f"{name}.gguf",
        relative_path=f"{name}.gguf",
        size_bytes=int(size_gb * 1024**3),
        size_gb=size_gb,
        modified_at="2026-06-25T00:00:00+00:00",
        family_hint="Qwen",
        parameter_hint=parameters_b,
        quantization_hint="Q4_K_M",
        metadata={"parameters_b": parameters_b, "quantization": "Q4_K_M"},
    )


class _FakeRegistry:
    def __init__(self, models):
        self.models = list(models)

    def scan_models(self):
        return list(self.models)

    def get_by_relative_path(self, relative_path):
        return next((item for item in self.models if item.relative_path == relative_path), None)

    def resolve_path(self, relative_path):
        model = self.get_by_relative_path(relative_path)
        if model is None:
            raise FileNotFoundError(relative_path)
        return Path(relative_path)


def _hardware(available_ram_gb: float):
    return HardwareInfo(
        operating_system="Test",
        architecture="x86_64",
        logical_cpu_count=8,
        physical_cpu_count=4,
        total_ram_gb=available_ram_gb,
        available_ram_gb=available_ram_gb,
        container_memory_limit_gb=None,
        gpu_backend="CPU",
        gpu_name=None,
        gpu_vram_gb=None,
    )


def test_auto_selection_chooses_safe_model_for_8gb(app, tmp_path: Path, monkeypatch):
    from app.services.ai.model_manager import ModelManager

    models = [
        _model_info("qwen-3b", size_gb=2.0, parameters_b=3),
        _model_info("qwen-7b", size_gb=4.5, parameters_b=7),
    ]
    registry = _FakeRegistry(models)
    with app.app_context():
        app.config.update(
            DATA_STORE_BACKEND="json",
            AI_SETTINGS_FILE=tmp_path / "ai_settings.json",
            AI_MODEL_PATH="",
        )
        AISettingsService().save(
            {
                "selection_mode": "auto",
                "auto_activate": True,
                "min_free_ram_gb": 2,
                "max_ram_usage_ratio": 0.72,
            }
        )
        manager = ModelManager(app)
        monkeypatch.setattr(manager, "registry", lambda: registry)
        monkeypatch.setattr(
            "app.services.ai.model_manager.HardwareInfoService.collect",
            lambda _self: _hardware(8),
        )
        selected = manager.ensure_auto_selection(force=True)

    assert selected.active_model_relative_path == "qwen-3b.gguf"
    assert selected.backend == "llama_cpp"
    assert selected.last_test_status == "pending"


def test_auto_selection_chooses_stronger_model_for_16gb(app, tmp_path: Path, monkeypatch):
    from app.services.ai.model_manager import ModelManager

    models = [
        _model_info("qwen-3b", size_gb=2.0, parameters_b=3),
        _model_info("qwen-7b", size_gb=4.5, parameters_b=7),
    ]
    registry = _FakeRegistry(models)
    with app.app_context():
        app.config.update(
            DATA_STORE_BACKEND="json",
            AI_SETTINGS_FILE=tmp_path / "ai_settings.json",
            AI_MODEL_PATH="",
        )
        AISettingsService().save(
            {
                "selection_mode": "auto",
                "auto_activate": True,
                "min_free_ram_gb": 2,
                "max_ram_usage_ratio": 0.72,
            }
        )
        manager = ModelManager(app)
        monkeypatch.setattr(manager, "registry", lambda: registry)
        monkeypatch.setattr(
            "app.services.ai.model_manager.HardwareInfoService.collect",
            lambda _self: _hardware(16),
        )
        selected = manager.ensure_auto_selection(force=True)

    assert selected.active_model_relative_path == "qwen-7b.gguf"
    assert selected.performance_profile in {"balanced", "performance"}


def test_auto_selected_model_is_tested_on_first_use_only(app, tmp_path: Path, monkeypatch):
    from app.services.ai.model_manager import ModelManager

    model_info = _model_info("qwen-3b", size_gb=2.0, parameters_b=3)
    registry = _FakeRegistry([model_info])
    completions = []

    class FakeLlama:
        def __init__(self, **_kwargs):
            self.closed = False

        def create_completion(self, **kwargs):
            completions.append(kwargs)
            return {"choices": [{"text": "OK"}]}

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=FakeLlama))

    with app.app_context():
        app.config.update(
            DATA_STORE_BACKEND="json",
            AI_SETTINGS_FILE=tmp_path / "ai_settings.json",
            AI_MODEL_PATH="",
        )
        AISettingsService().save(
            {
                "selection_mode": "auto",
                "auto_activate": True,
                "auto_test": True,
                "min_free_ram_gb": 2,
                "max_ram_usage_ratio": 0.72,
            }
        )
        manager = ModelManager(app)
        monkeypatch.setattr(manager, "registry", lambda: registry)
        monkeypatch.setattr(
            "app.services.ai.model_manager.HardwareInfoService.collect",
            lambda _self: _hardware(8),
        )
        manager.ensure_auto_selection(force=True)
        first = manager.get_active_model()
        second = manager.get_active_model()
        saved = AISettingsService().get()

    assert first is second
    assert len(completions) == 1
    assert saved.last_test_status == "success"
    assert saved.last_test_model_signature


def test_layout_exposes_sheetnorm_favicons(client):
    response = client.get("/")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "favicon.svg" in page
    assert "apple-touch-icon.png" in page
    assert "site.webmanifest" in page
    assert "by FormaOps" in page
