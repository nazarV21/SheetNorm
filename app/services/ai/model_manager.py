from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import gc
from hashlib import sha256
import json
import os
import threading
import time
from typing import Any, Iterable

from flask import Flask, current_app

from .hardware_info import HardwareInfo, HardwareInfoService
from .model_recommendation import ModelRecommendation, ModelRecommendationService
from .model_registry import LocalModelInfo, ModelRegistry
from .settings_service import AISettingsService, RuntimeAISettings


class ModelLoadError(RuntimeError):
    pass


class ModelManager:
    """Owns the single in-process llama.cpp model instance.

    Automatic mode assigns the safest and strongest GGUF for the current server
    without loading it at application startup. The model is loaded lazily on the
    first real AI request. If auto testing is enabled, that first load also
    performs a short validation and stores the result in persistent settings.
    """

    def __init__(self, app: Flask):
        self.app = app
        self._lock = threading.RLock()
        self._inference_lock = threading.Lock()
        self._model: Any = None
        self._loaded_signature: tuple[Any, ...] | None = None
        self._status = "unloaded"
        self._error = ""
        self._idle_timer: threading.Timer | None = None
        self._last_used_at: datetime | None = None
        self._loaded_at: datetime | None = None

    def registry(self) -> ModelRegistry:
        return ModelRegistry(self.app.config["AI_MODELS_DIR"])

    def get_runtime_settings(self) -> RuntimeAISettings:
        with self._context():
            return AISettingsService().get()

    def ensure_auto_selection(
        self,
        *,
        force: bool = False,
        excluded_paths: Iterable[str] = (),
    ) -> RuntimeAISettings:
        """Assign a model for the current hardware without loading it into RAM."""
        with self._context():
            service = AISettingsService()
            settings = service.get()
            if settings.selection_mode != "auto":
                return settings

            registry = self.registry()
            hardware = HardwareInfoService().collect()
            hardware_fingerprint = self._hardware_fingerprint(hardware)
            active = registry.get_by_relative_path(settings.active_model_relative_path)
            selected = registry.get_by_relative_path(settings.selected_model_relative_path)
            hardware_changed = bool(
                settings.hardware_fingerprint
                and settings.hardware_fingerprint != hardware_fingerprint
            )
            missing_assignment = active is None or selected is None
            if (
                not force
                and bool(settings.hardware_fingerprint)
                and not hardware_changed
                and not missing_assignment
                and settings.backend == "llama_cpp"
            ):
                return settings

            excluded = {str(item).replace("\\", "/") for item in excluded_paths}
            candidates: list[tuple[LocalModelInfo, ModelRecommendation]] = []
            recommender = ModelRecommendationService()
            available = hardware.effective_available_ram_gb
            for model in registry.scan_models():
                if model.relative_path in excluded or model.status == "unreadable":
                    continue
                recommendation = recommender.evaluate(model, hardware)
                if available is not None:
                    max_by_ratio = available * settings.max_ram_usage_ratio
                    max_by_reserve = max(0.0, available - settings.min_free_ram_gb)
                    safe_limit = min(max_by_ratio, max_by_reserve)
                    if recommendation.estimated_memory_required_gb > safe_limit:
                        continue
                elif recommendation.status == "heavy":
                    continue
                candidates.append((model, recommendation))

            if not candidates:
                reason = (
                    "В папке models нет модели, которая укладывается в безопасный лимит памяти. "
                    "Используется fallback без LLM."
                )
                if settings.reselect_if_unavailable or not settings.active_model_relative_path:
                    self.unload_model()
                    self._status = "fallback"
                    return service.save(
                        {
                            "backend": "fallback",
                            "selected_model_relative_path": None,
                            "active_model_relative_path": None,
                            "auto_selection_reason": reason,
                            "hardware_fingerprint": hardware_fingerprint,
                            "last_test_status": "not_tested",
                            "last_test_model_signature": "",
                        }
                    )
                return settings

            candidates.sort(key=self._candidate_sort_key, reverse=True)
            model, recommendation = candidates[0]
            profile = recommendation.recommended_profile
            profile_values = service.profile_values(
                profile,
                hardware.physical_cpu_count,
                bool(hardware.gpu_name),
            )
            reason = (
                f"Автоматически выбрана {model.display_name}: {recommendation.reason} "
                f"Профиль — {self._profile_label(profile)}."
            )
            previous_signature = settings.last_test_model_signature
            proposed = service.save(
                {
                    "backend": "llama_cpp" if settings.auto_activate else "fallback",
                    "selected_model_relative_path": model.relative_path,
                    "active_model_relative_path": model.relative_path if settings.auto_activate else None,
                    "performance_profile": profile,
                    **profile_values,
                    "auto_selection_reason": reason,
                    "hardware_fingerprint": hardware_fingerprint,
                }
            )
            current_signature = self._model_test_signature(model, proposed)
            if previous_signature != current_signature:
                proposed = service.save(
                    {
                        "last_test_status": "pending" if proposed.auto_test else "not_tested",
                        "last_test_message": (
                            "Модель назначена автоматически и будет проверена при первом AI-запросе."
                            if proposed.auto_test
                            else "Автоматический тест отключён."
                        ),
                        "last_tested_at": None,
                        "last_test_model_signature": "",
                    }
                )
            self.unload_model()
            self._status = "unloaded" if proposed.active_model_relative_path else "fallback"
            return proposed

    def get_active_model(self):
        settings = self.ensure_auto_selection()
        if settings.backend != "llama_cpp" or not settings.active_model_relative_path:
            self.unload_model()
            self._status = "fallback"
            return None

        attempted: set[str] = set()
        while settings.active_model_relative_path and settings.active_model_relative_path not in attempted:
            current_path = settings.active_model_relative_path
            attempted.add(current_path)
            signature = self._signature(settings)
            with self._lock:
                self._cancel_idle_timer_locked()
                if self._model is not None and signature == self._loaded_signature:
                    self._last_used_at = datetime.now(timezone.utc)
                    return self._model
                self._status = "loading"
                self._error = ""
                try:
                    model = self._create_llama(current_path, settings)
                    model_info = self.registry().get_by_relative_path(current_path)
                    if model_info is None:
                        raise ModelLoadError("Файл активной модели больше не найден в папке models.")
                    if settings.auto_test and not self._is_test_current(model_info, settings):
                        self._validate_loaded_model(model, model_info, settings)
                except Exception as exc:
                    self._close_model(locals().get("model"))
                    self._status = "error"
                    self._error = str(exc)
                    self.app.logger.warning("Local LLM could not be loaded: %s", exc)
                    if settings.selection_mode == "auto" and settings.reselect_if_unavailable:
                        settings = self.ensure_auto_selection(force=True, excluded_paths=attempted)
                        continue
                    return None
                self._close_current_model_locked()
                self._model = model
                self._loaded_signature = signature
                now = datetime.now(timezone.utc)
                self._loaded_at = now
                self._last_used_at = now
                self._status = "ready"
                return self._model
        return None

    def create_completion(self, **kwargs: Any) -> dict[str, Any]:
        with self._inference_lock:
            model = self.get_active_model()
            if model is None:
                raise ModelLoadError("Локальная модель недоступна.")
            try:
                return model.create_completion(**kwargs)
            finally:
                self.mark_used()

    def mark_used(self) -> None:
        settings = self.get_runtime_settings()
        with self._lock:
            self._last_used_at = datetime.now(timezone.utc)
            self._cancel_idle_timer_locked()
            if self._model is None or settings.memory_mode != "economy":
                return
            delay = max(30, int(settings.idle_unload_seconds or 300))
            timer = threading.Timer(delay, self._idle_unload, args=(delay,))
            timer.daemon = True
            self._idle_timer = timer
            timer.start()

    def _idle_unload(self, delay: int) -> None:
        with self._inference_lock:
            with self._lock:
                self._idle_timer = None
                if self._model is None or self._last_used_at is None:
                    return
                idle_seconds = (datetime.now(timezone.utc) - self._last_used_at).total_seconds()
                if idle_seconds + 0.5 < delay:
                    return
                self.app.logger.info(
                    "Unloading local LLM after %.1f seconds of inactivity", idle_seconds
                )
                self._close_current_model_locked()
                self._loaded_signature = None
                self._loaded_at = None
                self._status = "unloaded"

    def test_selected_model(self) -> dict[str, Any]:
        settings = self.get_runtime_settings()
        relative = settings.selected_model_relative_path
        if not relative:
            raise ModelLoadError("Сначала выберите GGUF-модель.")
        model_info = self.registry().get_by_relative_path(relative)
        if model_info is None:
            raise ModelLoadError("Выбранный файл модели не найден.")
        started = time.perf_counter()
        before = self._process_memory_mb()
        temporary_model = None
        self.unload_model()
        try:
            temporary_model = self._create_llama(relative, settings)
            load_time_ms = int((time.perf_counter() - started) * 1000)
            completion_started = time.perf_counter()
            completion = temporary_model.create_completion(
                prompt="Ответь одним словом: OK",
                temperature=0.0,
                max_tokens=8,
            )
            total_time_ms = int((time.perf_counter() - started) * 1000)
            response = str(completion.get("choices", [{}])[0].get("text", "")).strip()
            if not response:
                raise ModelLoadError("Модель загрузилась, но вернула пустой ответ.")
            result = {
                "status": "success",
                "model_relative_path": relative,
                "load_time_ms": load_time_ms,
                "response_time_ms": int((time.perf_counter() - completion_started) * 1000),
                "total_time_ms": total_time_ms,
                "response": response[:200],
                "memory_before_mb": before,
                "memory_after_mb": self._process_memory_mb(),
            }
            hardware = HardwareInfoService().collect()
            with self._context():
                AISettingsService().record_test(
                    success=True,
                    message=f"Тест пройден за {total_time_ms} мс. Ответ: {response[:80]}",
                    model_signature=self._model_test_signature(model_info, settings),
                    hardware_fingerprint=self._hardware_fingerprint(hardware),
                )
            return result
        except Exception as exc:
            with self._context():
                AISettingsService().record_test(success=False, message=str(exc))
            if isinstance(exc, ModelLoadError):
                raise
            raise ModelLoadError(str(exc)) from exc
        finally:
            self._close_model(temporary_model)
            gc.collect()

    def activate_selected_model(self) -> RuntimeAISettings:
        self.test_selected_model()
        with self._context():
            settings = AISettingsService().get()
            if not settings.selected_model_relative_path:
                raise ModelLoadError("Модель не выбрана.")
            settings = AISettingsService().save(
                {
                    "selection_mode": "manual",
                    "backend": "llama_cpp",
                    "active_model_relative_path": settings.selected_model_relative_path,
                    "auto_selection_reason": "Модель выбрана администратором вручную.",
                }
            )
        self.unload_model()
        return settings

    def set_fallback(self) -> RuntimeAISettings:
        with self._context():
            settings = AISettingsService().save(
                {
                    "selection_mode": "manual",
                    "backend": "fallback",
                    "active_model_relative_path": None,
                    "auto_selection_reason": "LLM отключена администратором.",
                }
            )
        self.unload_model()
        self._status = "fallback"
        return settings

    def unload_model(self) -> None:
        with self._lock:
            self._cancel_idle_timer_locked()
            self._close_current_model_locked()
            self._loaded_signature = None
            self._loaded_at = None
            if self._status != "fallback":
                self._status = "unloaded"

    def status(self) -> dict[str, Any]:
        settings = self.get_runtime_settings()
        active_info = self.registry().get_by_relative_path(settings.active_model_relative_path)
        selected_info = self.registry().get_by_relative_path(settings.selected_model_relative_path)
        next_unload_seconds = None
        if settings.memory_mode == "economy" and self._model is not None and self._last_used_at is not None:
            elapsed = (datetime.now(timezone.utc) - self._last_used_at).total_seconds()
            next_unload_seconds = max(0, int(settings.idle_unload_seconds - elapsed))
        if settings.backend == "fallback":
            human_status = "Используется fallback без LLM"
        elif self._model is not None and self._status == "ready":
            human_status = "Модель загружена и готова"
        elif settings.active_model_relative_path:
            human_status = "Модель назначена и загрузится при первом AI-запросе"
        else:
            human_status = "Модель для AI не назначена"
        return {
            "process_id": os.getpid(),
            "status": self._status,
            "status_label": human_status,
            "error": self._error,
            "backend": settings.backend,
            "selection_mode": settings.selection_mode,
            "selected_model_relative_path": settings.selected_model_relative_path,
            "selected_model_name": selected_info.display_name if selected_info else None,
            "active_model_relative_path": settings.active_model_relative_path,
            "active_model_name": active_info.display_name if active_info else None,
            "performance_profile": settings.performance_profile,
            "memory_mode": settings.memory_mode,
            "idle_unload_seconds": settings.idle_unload_seconds,
            "next_unload_seconds": next_unload_seconds,
            "loaded": self._model is not None,
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "last_used_at": self._last_used_at.isoformat() if self._last_used_at else None,
            "process_memory_mb": self._process_memory_mb(),
        }

    def _validate_loaded_model(
        self,
        model: Any,
        model_info: LocalModelInfo,
        settings: RuntimeAISettings,
    ) -> None:
        started = time.perf_counter()
        completion = model.create_completion(
            prompt="Ответь одним словом: OK",
            temperature=0.0,
            max_tokens=8,
        )
        response = str(completion.get("choices", [{}])[0].get("text", "")).strip()
        if not response:
            raise ModelLoadError("Автоматическая проверка модели вернула пустой ответ.")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        hardware = HardwareInfoService().collect()
        with self._context():
            AISettingsService().record_test(
                success=True,
                message=f"Автоматическая проверка пройдена за {elapsed_ms} мс.",
                model_signature=self._model_test_signature(model_info, settings),
                hardware_fingerprint=self._hardware_fingerprint(hardware),
            )

    def _is_test_current(self, model_info: LocalModelInfo, settings: RuntimeAISettings) -> bool:
        return (
            settings.last_test_status == "success"
            and settings.last_test_model_signature == self._model_test_signature(model_info, settings)
        )

    def _create_llama(self, relative_path: str, settings: RuntimeAISettings):
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise ModelLoadError(
                "llama-cpp-python не установлен. Установите requirements-ai.txt."
            ) from exc
        model_path = self.registry().resolve_path(relative_path)
        return Llama(
            model_path=str(model_path),
            n_ctx=settings.context_tokens,
            n_threads=settings.n_threads,
            n_batch=settings.n_batch,
            n_gpu_layers=settings.n_gpu_layers,
            verbose=False,
        )

    def _context(self):
        try:
            current_app._get_current_object()
            return nullcontext()
        except RuntimeError:
            return self.app.app_context()

    @staticmethod
    def _signature(settings: RuntimeAISettings) -> tuple[Any, ...]:
        return (
            settings.active_model_relative_path,
            settings.context_tokens,
            settings.n_threads,
            settings.n_batch,
            settings.n_gpu_layers,
        )

    @staticmethod
    def _candidate_sort_key(
        item: tuple[LocalModelInfo, ModelRecommendation],
    ) -> tuple[int, float, int, float]:
        model, recommendation = item
        status_score = {
            "recommended": 4,
            "acceptable": 3,
            "unknown": 2,
            "heavy": 1,
        }.get(recommendation.status, 0)
        quant = (model.quantization_hint or "").upper()
        quant_score = 3 if "Q4_K_M" in quant else 2 if quant.startswith("Q4") else 1
        return (
            status_score,
            float(model.parameter_hint or 0.0),
            quant_score,
            float(model.size_gb),
        )

    @staticmethod
    def _hardware_fingerprint(hardware: HardwareInfo) -> str:
        stable = {
            "architecture": hardware.architecture,
            "logical_cpu_count": hardware.logical_cpu_count,
            "physical_cpu_count": hardware.physical_cpu_count,
            "total_ram_gb": hardware.total_ram_gb,
            "container_memory_limit_gb": hardware.container_memory_limit_gb,
            "gpu_backend": hardware.gpu_backend,
            "gpu_name": hardware.gpu_name,
            "gpu_vram_gb": hardware.gpu_vram_gb,
        }
        return sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _model_test_signature(model: LocalModelInfo, settings: RuntimeAISettings) -> str:
        raw = "|".join(
            [
                model.relative_path,
                str(model.size_bytes),
                model.modified_at,
                str(settings.context_tokens),
                str(settings.n_threads),
                str(settings.n_batch),
                str(settings.n_gpu_layers),
            ]
        )
        return sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _profile_label(profile: str) -> str:
        return {
            "economy": "экономичный",
            "balanced": "сбалансированный",
            "performance": "производительный",
            "custom": "пользовательский",
        }.get(profile, profile)

    def _cancel_idle_timer_locked(self) -> None:
        timer = self._idle_timer
        self._idle_timer = None
        if timer is not None:
            timer.cancel()

    def _close_current_model_locked(self) -> None:
        self._close_model(self._model)
        self._model = None
        gc.collect()

    @staticmethod
    def _close_model(model: Any) -> None:
        if model is None:
            return
        close = getattr(model, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @staticmethod
    def _process_memory_mb() -> float | None:
        try:
            import psutil

            return round(psutil.Process().memory_info().rss / (1024**2), 1)
        except Exception:
            return None


def get_model_manager() -> ModelManager:
    app = current_app._get_current_object()
    manager = app.extensions.get("sheetnorm_model_manager")
    if manager is None:
        manager = ModelManager(app)
        app.extensions["sheetnorm_model_manager"] = manager
    return manager
