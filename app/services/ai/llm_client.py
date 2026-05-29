from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

import pandas as pd
from flask import current_app

try:
    from llama_cpp import Llama
except ImportError:  # pragma: no cover - llama-cpp optional на dev
    Llama = None  # type: ignore


@dataclass
class AIClient:
    """Адаптер для взаимодействия с локальной/удалённой LLM."""

    def __post_init__(self):
        self.backend = current_app.config.get("AI_BACKEND", "llama_cpp")
        self._llm = None
        # Пытаемся подключить локальную модель только если файл реально существует.
        if self.backend == "llama_cpp" and Llama:
            model_path = Path(current_app.config["AI_MODEL_PATH"])
            if model_path.exists():
                try:
                    self._llm = Llama(
                        model_path=str(model_path),
                        n_ctx=4096,
                        n_threads=6,
                        verbose=False,
                    )
                except Exception as exc:
                    current_app.logger.warning("LLM model was found but could not be loaded: %s", exc)

    def suggest_schema(self, df: pd.DataFrame) -> dict[str, Any]:
        preview = df.head(10).to_dict(orient="records")
        prompt = self._build_prompt(preview)

        if self._llm:
            completion = self._llm.create_completion(
                prompt=prompt,
                temperature=0.1,
                max_tokens=512,
            )
            text = completion["choices"][0]["text"]
            return {"engine": self.backend, "suggestion": text}

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
        """
        Применить текстовый промпт пользователя к таблице.
        ИИ читает описание и выполняет преобразования.
        
        Args:
            df: DataFrame с данными (может быть уже с заголовками)
            user_prompt: Текстовое описание задачи от пользователя
            raw_data: Сырые данные без заголовков (для сложных структур типа кросс-таблиц)
            training_examples: Список обучающих примеров (пар исходная-итоговая таблица)
        """
        # Добавляем обучающие примеры в промпт (few-shot learning)
        examples_section = ""
        if training_examples:
            examples_section = "\n\n=== ОБУЧАЮЩИЕ ПРИМЕРЫ ===\n"
            examples_section += "Ниже приведены примеры правильных преобразований. Используй их как образец:\n\n"
            
            max_examples = int(current_app.config.get("AI_MAX_TRAINING_EXAMPLES", 8))
            for i, example in enumerate(training_examples[:max_examples], 1):
                try:
                    source_path = Path(example.get("source_path", ""))
                    target_path = Path(example.get("target_path", ""))
                    
                    if source_path.exists() and target_path.exists():
                        df_source_ex = pd.read_excel(source_path, header=None)
                        df_target_ex = pd.read_excel(target_path)
                        
                        examples_section += f"Пример {i}:\n"
                        examples_section += f"Исходная таблица (первые 15 строк):\n{df_source_ex.head(15).to_string()}\n\n"
                        examples_section += f"Итоговая таблица (первые 10 строк):\n{df_target_ex.head(10).to_string()}\n\n"
                        if example.get("prompt"):
                            examples_section += f"Промпт для этого примера: {example['prompt']}\n"
                        examples_section += "---\n\n"
                except Exception:
                    continue  # Пропускаем примеры с ошибками
        
        # Если есть сырые данные, используем их для анализа структуры
        if raw_data is not None:
            # Передаём ИИ сырые данные для анализа сложной структуры
            preview_raw = raw_data.head(30).to_string()
            # Также передаём информацию о структуре (индексы столбцов)
            columns_info = f"Столбцы таблицы (индексы 0-{len(raw_data.columns)-1}): {list(range(len(raw_data.columns)))}\n"
            
            system_prompt = (
                "Ты — эксперт по преобразованию сложных таблиц Excel в плоские структуры.\n\n"
                f"{examples_section}\n"
                "ИНСТРУКЦИЯ ПО ПРЕОБРАЗОВАНИЮ:\n"
                "1. Проанализируй структуру исходной таблицы (строки, столбцы, заголовки)\n"
                "2. Пойми, какие данные где находятся\n"
                "3. Определи, как развернуть кросс-таблицу в плоскую структуру\n"
                "4. Создай итоговую таблицу с правильными колонками и данными\n\n"
                f"СТРУКТУРА ТАБЛИЦЫ:\n{columns_info}\n"
                f"СЫРЫЕ ДАННЫЕ (первые 30 строк):\n{preview_raw}\n\n"
                f"ЗАДАЧА: {user_prompt}\n\n"
                "ВАЖНО: Ответь ТОЛЬКО валидным JSON без дополнительного текста:\n"
                "{\n"
                '  "action": "transform",\n'
                '  "result_columns": ["точные", "названия", "колонок", "итоговой таблицы"],\n'
                '  "steps": [\n'
                '    {"step": 1, "description": "что делать", "row_from": 5, "row_to": null, "col_from": 0, "col_to": 4},\n'
                '    {"step": 2, "description": "что делать дальше"}\n'
                '  ],\n'
                '  "mapping": {\n'
                '    "Блок-вставки по КРС": "константа: БВ-2.3а или из строки 0, колонка 0",\n'
                '    "Наименование работ": "из колонки 1, объединить многострочные",\n'
                '    "Группы подъемных агрегатов": "из строки 4, колонки 2,3,4",\n'
                '    "Норма времени, час": "значения из ячеек колонок 2,3,4"\n'
                '  }\n'
                "}"
            )
        else:
            preview = df.head(20).to_dict(orient="records")
            columns_info = ", ".join(df.columns.tolist())
            
            system_prompt = (
                "Ты — эксперт по преобразованию таблиц Excel. Твоя задача — точно выполнить преобразование.\n\n"
                f"{examples_section}\n"
                "ДАННЫЕ ДЛЯ ПРЕОБРАЗОВАНИЯ:\n"
                f"Столбцы: {columns_info}\n"
                f"Примеры строк (первые 20):\n{json.dumps(preview, ensure_ascii=False, indent=2)}\n\n"
                f"ЗАДАЧА ПОЛЬЗОВАТЕЛЯ: {user_prompt}\n\n"
                "ОТВЕТЬ ТОЛЬКО ВАЛИДНЫМ JSON БЕЗ ДОПОЛНИТЕЛЬНОГО ТЕКСТА:\n"
                "{\n"
                '  "action": "transform",\n'
                '  "rename": {"старое_имя_колонки": "новое_имя_колонки"},\n'
                '  "select_columns": ["список", "колонок", "для", "выбора"],\n'
                '  "calculated": [\n'
                '    {"name": "имя_новой_колонки", "formula": "выражение на pandas/python"}\n'
                '  ],\n'
                '  "filter": {"condition": "опционально, условие фильтрации"}\n'
                "}\n"
                "Если поле не нужно, верни null или пустой массив."
            )
        
        if self._llm:
            completion = self._llm.create_completion(
                prompt=system_prompt,
                temperature=0.2,
                max_tokens=2048,  # Увеличено для сложных преобразований
            )
            text = completion["choices"][0]["text"]
            # Пытаемся распарсить JSON из ответа
            import re
            # Пытаемся найти JSON в ответе (может быть в markdown блоке или просто в тексте)
            json_patterns = [
                r'```json\s*(\{[\s\S]*?\})\s*```',
                r'```\s*(\{[\s\S]*?\})\s*```',
                r'(\{[\s\S]*?\})',
            ]
            
            schema = None
            for pattern in json_patterns:
                json_match = re.search(pattern, text, re.DOTALL)
                if json_match:
                    try:
                        schema = json.loads(json_match.group(1))
                        break
                    except json.JSONDecodeError:
                        continue
            
            if schema:
                try:
                    return self._apply_advanced_schema(df.copy() if raw_data is None else raw_data.copy(), schema, user_prompt)
                except Exception:
                    pass  # Если не удалось применить схему, продолжаем с fallback
        
        # Фоллбек: если ИИ не справился, возвращаем исходную таблицу
        # (специальный конвертер можно использовать как дополнительный fallback)
        if raw_data is not None:
            return raw_data
        
        return df
    
    def _apply_advanced_schema(self, df: pd.DataFrame, schema: dict[str, Any], user_prompt: str) -> pd.DataFrame:
        """Применить расширенную схему преобразований к DataFrame (включая разворачивание)."""
        # Базовые преобразования
        df = self._apply_schema_to_df(df, schema)
        
        # Разворачивание кросс-таблиц через melt
        if "melt_columns" in schema and "id_vars" in schema:
            try:
                id_vars = schema.get("id_vars", [])
                value_vars = schema.get("melt_columns", [])
                var_name = schema.get("var_name", "Группы подъемных агрегатов")
                value_name = schema.get("value_name", "Норма времени, час")
                
                df = pd.melt(
                    df,
                    id_vars=id_vars,
                    value_vars=value_vars,
                    var_name=var_name,
                    value_name=value_name
                )
            except Exception:
                pass
        
        # Если есть инструкция на создание нового столбца с постоянным значением
        if "constant_columns" in schema:
            for col_info in schema["constant_columns"]:
                col_name = col_info.get("name")
                col_value = col_info.get("value")
                if col_name and col_value is not None:
                    df[col_name] = col_value
        
        return df
    
    def _fallback_cross_table_transform(self, raw_df: pd.DataFrame, user_prompt: str) -> pd.DataFrame:
        """Простое fallback-преобразование для кросс-таблиц без ИИ."""
        # Если ИИ не сработал, возвращаем исходные данные
        # В будущем здесь можно добавить простую эвристику
        return raw_df
    
    def _apply_schema_to_df(self, df: pd.DataFrame, schema: dict[str, Any]) -> pd.DataFrame:
        """Применить схему преобразований к DataFrame."""
        rename_map = schema.get("rename", {})
        if rename_map:
            df = df.rename(columns=rename_map)
        
        select_cols = schema.get("columns")
        if select_cols:
            df = df[[col for col in select_cols if col in df.columns]]
        
        calculated = schema.get("calculated", [])
        for rule in calculated:
            target = rule.get("name")
            expr = rule.get("expr")
            if target and expr:
                try:
                    df[target] = df.eval(expr)
                except Exception:
                    pass  # игнорируем ошибки вычислений
        
        return df
