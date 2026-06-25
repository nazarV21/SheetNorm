from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db


@dataclass
class RuntimeAISettings:
    backend: str = "fallback"
    selected_model_relative_path: str | None = None
    active_model_relative_path: str | None = None
    selection_mode: str = "auto"
    auto_activate: bool = True
    auto_test: bool = True
    reselect_if_unavailable: bool = True
    min_free_ram_gb: float = 2.0
    max_ram_usage_ratio: float = 0.72
    auto_selection_reason: str = ""
    hardware_fingerprint: str = ""
    performance_profile: str = "balanced"
    context_tokens: int = 4096
    max_completion_tokens: int = 1000
    n_threads: int = 4
    n_batch: int = 128
    n_gpu_layers: int = 0
    temperature: float = 0.15
    memory_mode: str = "economy"
    idle_unload_seconds: int = 300
    last_test_status: str = "not_tested"
    last_test_message: str = ""
    last_tested_at: str | None = None
    last_test_model_signature: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AISettingsService:
    GLOBAL_ID = "global"
    PROFILES = {"economy", "balanced", "performance", "custom"}
    MEMORY_MODES = {"economy", "fast", "manual"}
    SELECTION_MODES = {"auto", "manual"}

    def get(self) -> RuntimeAISettings:
        defaults = self._defaults_from_config()
        file_payload = self._read_file()
        fallback = self._from_mapping(file_payload, defaults)
        if current_app.config.get("DATA_STORE_BACKEND") == "database" and self._database_schema_ready():
            try:
                from app.db.models import AISettings

                row = db.session.get(AISettings, self.GLOBAL_ID)
                if row is None:
                    row = AISettings(id=self.GLOBAL_ID, **self._model_kwargs(fallback))
                    db.session.add(row)
                    db.session.commit()
                return self._from_mapping(
                    {column.name: getattr(row, column.name) for column in row.__table__.columns},
                    fallback,
                )
            except SQLAlchemyError as exc:
                db.session.rollback()
                current_app.logger.warning(
                    "AI settings database is unavailable; using file/config fallback: %s", exc
                )
        return fallback

    def save(self, values: dict[str, Any]) -> RuntimeAISettings:
        current = self.get()
        merged = {**current.as_dict(), **values}
        validated = self._validate(merged)
        if current_app.config.get("DATA_STORE_BACKEND") == "database" and self._database_schema_ready():
            try:
                from app.db.models import AISettings

                row = db.session.get(AISettings, self.GLOBAL_ID)
                if row is None:
                    row = AISettings(id=self.GLOBAL_ID)
                    db.session.add(row)
                for key, value in self._model_kwargs(validated).items():
                    setattr(row, key, value)
                db.session.commit()
                return validated
            except SQLAlchemyError as exc:
                db.session.rollback()
                current_app.logger.warning(
                    "Could not persist AI settings in database; writing file fallback: %s", exc
                )
        self._write_file(validated)
        return validated

    def record_test(
        self,
        *,
        success: bool,
        message: str,
        model_signature: str = "",
        hardware_fingerprint: str | None = None,
    ) -> RuntimeAISettings:
        values: dict[str, Any] = {
            "last_test_status": "success" if success else "failed",
            "last_test_message": message[:2000],
            "last_tested_at": datetime.now(timezone.utc).isoformat(),
            "last_test_model_signature": model_signature if success else "",
        }
        if hardware_fingerprint is not None:
            values["hardware_fingerprint"] = hardware_fingerprint
        return self.save(values)

    def profile_values(self, profile: str, physical_cpu_count: int, has_gpu: bool) -> dict[str, Any]:
        cores = max(1, int(physical_cpu_count or 1))
        if profile == "economy":
            return {
                "context_tokens": 2048,
                "max_completion_tokens": 700,
                "n_threads": min(4, cores),
                "n_batch": 64,
                "n_gpu_layers": 0,
                "temperature": 0.15,
            }
        if profile == "performance":
            return {
                "context_tokens": 8192,
                "max_completion_tokens": 1400,
                "n_threads": min(16, cores),
                "n_batch": 256,
                "n_gpu_layers": -1 if has_gpu else 0,
                "temperature": 0.15,
            }
        return {
            "context_tokens": 4096,
            "max_completion_tokens": 1000,
            "n_threads": min(8, cores),
            "n_batch": 128,
            "n_gpu_layers": 0,
            "temperature": 0.15,
        }

    def _defaults_from_config(self) -> RuntimeAISettings:
        configured_path = str(current_app.config.get("AI_MODEL_PATH") or "").replace("\\", "/")
        models_dir = Path(current_app.config.get("AI_MODELS_DIR", "models")).resolve()
        selected = None
        if configured_path:
            try:
                candidate = Path(configured_path).expanduser().resolve()
                if candidate.is_relative_to(models_dir):
                    selected = candidate.relative_to(models_dir).as_posix()
            except (OSError, ValueError):
                selected = None
        cpu_count = int(current_app.config.get("AI_N_THREADS") or 4)
        configured_profile = str(current_app.config.get("AI_DEFAULT_PROFILE", "balanced"))
        if configured_profile not in self.PROFILES:
            configured_profile = "balanced"
        selection_mode = str(current_app.config.get("AI_MODEL_SELECTION_MODE", "auto"))
        if selection_mode not in self.SELECTION_MODES:
            selection_mode = "auto"
        return RuntimeAISettings(
            backend=str(current_app.config.get("AI_BACKEND", "fallback")),
            selected_model_relative_path=selected,
            active_model_relative_path=selected if current_app.config.get("AI_BACKEND") == "llama_cpp" else None,
            selection_mode=selection_mode,
            auto_activate=bool(current_app.config.get("AI_AUTO_ACTIVATE", True)),
            auto_test=bool(current_app.config.get("AI_AUTO_TEST", True)),
            reselect_if_unavailable=bool(current_app.config.get("AI_RESELECT_IF_UNAVAILABLE", True)),
            min_free_ram_gb=float(current_app.config.get("AI_MIN_FREE_RAM_GB", 2.0)),
            max_ram_usage_ratio=float(current_app.config.get("AI_MAX_RAM_USAGE_RATIO", 0.72)),
            performance_profile=configured_profile,
            context_tokens=int(current_app.config.get("AI_CONTEXT_TOKENS", 4096)),
            max_completion_tokens=int(current_app.config.get("AI_MAX_COMPLETION_TOKENS", 1000)),
            n_threads=cpu_count,
            n_batch=int(current_app.config.get("AI_N_BATCH", 128)),
            n_gpu_layers=int(current_app.config.get("AI_N_GPU_LAYERS", 0)),
            temperature=float(current_app.config.get("AI_TEMPERATURE", 0.15)),
            memory_mode=str(current_app.config.get("AI_MEMORY_MODE", "economy")),
            idle_unload_seconds=int(current_app.config.get("AI_IDLE_UNLOAD_SECONDS", 300)),
        )

    def _validate(self, values: dict[str, Any]) -> RuntimeAISettings:
        backend = str(values.get("backend") or "fallback")
        if backend not in {"fallback", "llama_cpp"}:
            raise ValueError("Unknown AI backend")
        selection_mode = str(values.get("selection_mode") or "auto")
        if selection_mode not in self.SELECTION_MODES:
            raise ValueError("Unknown AI model selection mode")
        profile = str(values.get("performance_profile") or "balanced")
        if profile not in self.PROFILES:
            raise ValueError("Unknown performance profile")
        context = self._bounded_int(values.get("context_tokens"), 512, 32768, "context_tokens")
        completion = self._bounded_int(
            values.get("max_completion_tokens"), 64, 8192, "max_completion_tokens"
        )
        threads = self._bounded_int(values.get("n_threads"), 1, 128, "n_threads")
        batch = self._bounded_int(values.get("n_batch"), 16, 2048, "n_batch")
        gpu_layers = self._bounded_int(values.get("n_gpu_layers"), -1, 999, "n_gpu_layers")
        temperature = float(values.get("temperature", 0.15))
        memory_mode = str(values.get("memory_mode") or "economy")
        if memory_mode not in self.MEMORY_MODES:
            raise ValueError("Unknown AI memory mode")
        idle_unload_seconds = self._bounded_int(
            values.get("idle_unload_seconds", 300), 30, 86400, "idle_unload_seconds"
        )
        min_free_ram_gb = float(values.get("min_free_ram_gb", 2.0))
        max_ram_usage_ratio = float(values.get("max_ram_usage_ratio", 0.72))
        if not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if not 0 <= min_free_ram_gb <= 256:
            raise ValueError("min_free_ram_gb must be between 0 and 256")
        if not 0.1 <= max_ram_usage_ratio <= 0.95:
            raise ValueError("max_ram_usage_ratio must be between 0.1 and 0.95")
        return RuntimeAISettings(
            backend=backend,
            selected_model_relative_path=self._clean_relative(values.get("selected_model_relative_path")),
            active_model_relative_path=self._clean_relative(values.get("active_model_relative_path")),
            selection_mode=selection_mode,
            auto_activate=self._as_bool(values.get("auto_activate", True)),
            auto_test=self._as_bool(values.get("auto_test", True)),
            reselect_if_unavailable=self._as_bool(values.get("reselect_if_unavailable", True)),
            min_free_ram_gb=min_free_ram_gb,
            max_ram_usage_ratio=max_ram_usage_ratio,
            auto_selection_reason=str(values.get("auto_selection_reason") or "")[:2000],
            hardware_fingerprint=str(values.get("hardware_fingerprint") or "")[:128],
            performance_profile=profile,
            context_tokens=context,
            max_completion_tokens=completion,
            n_threads=threads,
            n_batch=batch,
            n_gpu_layers=gpu_layers,
            temperature=temperature,
            memory_mode=memory_mode,
            idle_unload_seconds=idle_unload_seconds,
            last_test_status=str(values.get("last_test_status") or "not_tested"),
            last_test_message=str(values.get("last_test_message") or ""),
            last_tested_at=str(values.get("last_tested_at") or "") or None,
            last_test_model_signature=str(values.get("last_test_model_signature") or "")[:256],
        )


    @staticmethod
    def _database_schema_ready() -> bool:
        try:
            from app.db.schema_compat import REQUIRED_AI_SETTINGS_COLUMNS, schema_has_columns

            return schema_has_columns("ai_settings", REQUIRED_AI_SETTINGS_COLUMNS)
        except Exception:
            return False

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int, name: str) -> int:
        parsed = int(value)
        if parsed < minimum or parsed > maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return parsed

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _clean_relative(value: Any) -> str | None:
        if value in {None, ""}:
            return None
        normalised = str(value).replace("\\", "/").strip("/")
        if not normalised or ".." in Path(normalised).parts or Path(normalised).is_absolute():
            raise ValueError("Invalid relative model path")
        return normalised

    @staticmethod
    def _from_mapping(payload: dict[str, Any] | None, defaults: RuntimeAISettings) -> RuntimeAISettings:
        service = AISettingsService()
        return service._validate({**defaults.as_dict(), **(payload or {})})

    @staticmethod
    def _model_kwargs(settings: RuntimeAISettings) -> dict[str, Any]:
        data = settings.as_dict()
        tested_at = data.get("last_tested_at")
        if isinstance(tested_at, str) and tested_at:
            try:
                data["last_tested_at"] = datetime.fromisoformat(tested_at)
            except ValueError:
                data["last_tested_at"] = None
        return data

    def _settings_file(self) -> Path:
        return Path(current_app.config.get("AI_SETTINGS_FILE")).resolve()

    def _read_file(self) -> dict[str, Any]:
        path = self._settings_file()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write_file(self, settings: RuntimeAISettings) -> None:
        path = self._settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(settings.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
