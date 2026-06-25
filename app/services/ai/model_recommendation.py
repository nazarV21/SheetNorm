from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .hardware_info import HardwareInfo
from .model_registry import LocalModelInfo


@dataclass(frozen=True)
class ModelRecommendation:
    status: str
    label: str
    reason: str
    estimated_memory_required_gb: float
    recommended_profile: str
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["warnings"] = list(self.warnings)
        return data


class ModelRecommendationService:
    def evaluate(self, model: LocalModelInfo, hardware: HardwareInfo) -> ModelRecommendation:
        metadata = model.metadata or {}
        context = int(metadata.get("recommended_context") or 4096)
        file_memory = max(model.size_gb * 1.3, model.size_gb + 0.6)
        context_reserve = max(0.5, min(context / 4096 * 0.8, 4.0))
        application_reserve = 1.0
        estimated = round(file_memory + context_reserve + application_reserve, 1)
        minimum = metadata.get("minimum_ram_gb")
        recommended = metadata.get("recommended_ram_gb")
        if isinstance(minimum, (int, float)):
            estimated = max(estimated, float(minimum))
        available = hardware.effective_available_ram_gb
        warnings: list[str] = []
        profile = "balanced"
        if model.parameter_hint and model.parameter_hint >= 10:
            profile = "economy"
        if hardware.gpu_vram_gb and hardware.gpu_vram_gb >= model.size_gb * 1.15:
            profile = "performance"
        if available is None:
            return ModelRecommendation(
                status="unknown",
                label="Нужен пробный запуск",
                reason=f"Оценка памяти: около {estimated} ГБ. Доступную RAM определить не удалось.",
                estimated_memory_required_gb=estimated,
                recommended_profile=profile,
                warnings=("Фактическое потребление зависит от контекста и сборки llama.cpp.",),
            )
        if isinstance(recommended, (int, float)) and available < float(recommended):
            warnings.append(f"В metadata рекомендуется не менее {recommended} ГБ RAM.")
        ratio = estimated / max(available, 0.1)
        if ratio <= 0.7:
            status, label = "recommended", "Подходит"
            reason = f"Оценочно требуется {estimated} ГБ при доступных {available} ГБ."
        elif ratio <= 0.92:
            status, label = "acceptable", "Можно попробовать"
            reason = f"Модель займёт значительную часть памяти: около {estimated} из {available} ГБ."
            warnings.append("Закройте другие тяжёлые приложения перед загрузкой.")
            profile = "economy"
        else:
            status, label = "heavy", "Может не хватить памяти"
            reason = f"Оценочно требуется {estimated} ГБ, доступно около {available} ГБ."
            warnings.append("Выберите меньшую модель или более сильную квантизацию.")
            profile = "economy"
        warnings.append("Оценка приблизительная и не заменяет тест загрузки.")
        return ModelRecommendation(status, label, reason, estimated, profile, tuple(warnings))
