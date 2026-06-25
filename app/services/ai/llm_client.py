from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json

import pandas as pd
from flask import current_app

from app.services.ai.model_manager import get_model_manager


@dataclass
class AIClient:
    """Адаптер для взаимодействия с локальной/удалённой LLM."""

    def __post_init__(self):
        manager = get_model_manager()
        self._model_manager = manager
        settings = manager.get_runtime_settings()
        self.backend = settings.backend
        self.context_tokens = settings.context_tokens
        self.max_completion_tokens = settings.max_completion_tokens
        self.temperature = settings.temperature
        # Do not load a local model in the constructor. ConversionService creates
        # this adapter for ordinary rule-based jobs as well; eager loading kept
        # 3-8 GB of RAM occupied even when no AI operation was requested.
        self._llm = None

    def suggest_schema(self, df: pd.DataFrame) -> dict[str, Any]:
        preview = df.head(10).to_dict(orient="records")
        prompt = self._build_prompt(preview)

        if self.backend == "llama_cpp":
            try:
                completion = self._model_manager.create_completion(
                    prompt=prompt,
                    temperature=self.temperature,
                    max_tokens=min(512, self.max_completion_tokens),
                )
                text = completion["choices"][0]["text"]
                return {"engine": self.backend, "suggestion": text}
            except Exception as exc:
                current_app.logger.warning("Local LLM schema suggestion failed; using fallback: %s", exc)

        # Фоллбек без LLM: простая эвристика
        suggestion = {
            "rename": {col: col.lower().strip().replace(" ", "_") for col in df.columns},
            "columns": list(df.columns),
        }
        return {"engine": "fallback", "suggestion": suggestion}

    def _build_prompt(self, preview: list[dict[str, Any]]) -> str:
        return (
            "Ты — помощник по обработке таблиц. На входе данные Excel.\n"
            "Сформируй JSON со структурой {rename, columns, calculated}.\n"
            f"Примеры строк: {preview}\n"
            "Ответь только валидным JSON."
        )
    
    def apply_prompt(
        self,
        df: pd.DataFrame,
        user_prompt: str,
        raw_data: pd.DataFrame | None = None,
        training_examples: list[dict[str, Any]] | None = None,
    ) -> pd.DataFrame:
        """Transformation execution is deliberately centralised in ConversionService.

        AI components may propose a validated declarative rule or an approved
        pandas script, but they must never mutate a DataFrame directly or silently
        return the source table as a successful result.
        """
        raise RuntimeError(
            "AIClient.apply_prompt is disabled. AI may generate a declarative rule or script, "
            "but ConversionService is the only component allowed to apply transformations."
        )
