"""Умный конвертер, который использует обучающие примеры для прямого преобразования."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import pandas as pd


class SmartConverter:
    """
    Конвертер, который анализирует обучающие примеры и применяет
    аналогичные преобразования к новым данным.
    """

    def convert_with_examples(
        self,
        source_df: pd.DataFrame,
        training_examples: list[dict[str, Any]],
        user_prompt: str,
        raw_data: pd.DataFrame | None = None,
    ) -> pd.DataFrame | None:
        """
        Преобразовать данные, используя обучающие примеры как образец.
        
        Args:
            source_df: Исходные данные (с заголовками)
            training_examples: Список обучающих примеров
            user_prompt: Описание задачи от пользователя
            raw_data: Сырые данные без заголовков (для кросс-таблиц)
        """
        # Если есть обучающие примеры, используем их для анализа паттерна.
        # Если паттерн не удалось извлечь, возвращаем None, чтобы ConversionService
        # продолжил обычный ИИ/fallback-пайплайн, а не сохранял исходную таблицу как результат.
        if training_examples:
            return self._convert_with_training_pattern(source_df, training_examples, raw_data)

        return None

    def _convert_with_training_pattern(
        self,
        source_df: pd.DataFrame,
        training_examples: list[dict[str, Any]],
        raw_data: pd.DataFrame | None = None,
    ) -> pd.DataFrame | None:
        """Преобразовать данные, анализируя паттерн из обучающих примеров."""
        
        # Берем первый пример как образец
        example = training_examples[0]
        source_path = Path(example.get("source_path", ""))
        target_path = Path(example.get("target_path", ""))
        
        if not source_path.exists() or not target_path.exists():
            return None
        
        try:
            # Читаем примеры
            if raw_data is not None:
                # Для кросс-таблиц используем сырые данные
                df_example_source = pd.read_excel(source_path, header=None)
                df_example_target = pd.read_excel(target_path)
                df_current = raw_data.copy()
            else:
                df_example_source = pd.read_excel(source_path)
                df_example_target = pd.read_excel(target_path)
                df_current = source_df.copy()
            
            # Анализируем структуру примеров
            # Если это кросс-таблица, используем специальный метод
            if self._is_cross_table(df_example_source):
                return self._convert_cross_table_pattern(
                    df_current, df_example_source, df_example_target
                )
            else:
                # Для обычных таблиц применяем сопоставление колонок
                return self._convert_simple_pattern(df_current, df_example_source, df_example_target)
                
        except Exception:
            # Если что-то пошло не так, даём основному пайплайну шанс обработать файл
            return None

    def _is_cross_table(self, df: pd.DataFrame) -> bool:
        """Проверить, является ли таблица кросс-таблицей."""
        # Простая эвристика: если много столбцов и мало строк, или структура с заголовками на нескольких строках
        if len(df) < 10 and len(df.columns) > 5:
            return True
        # Проверяем наличие заголовков на нескольких строках
        if isinstance(df.columns, pd.RangeIndex):
            # Если нет названий столбцов, возможно это сырые данные кросс-таблицы
            return True
        return False

    def _convert_cross_table_pattern(
        self,
        current_df: pd.DataFrame,
        example_source: pd.DataFrame,
        example_target: pd.DataFrame,
    ) -> pd.DataFrame:
        """Преобразовать кросс-таблицу, используя пример как образец."""
        
        # Анализируем структуру целевой таблицы из примера
        target_columns = list(example_target.columns)
        
        # Определяем структуру исходной таблицы
        # Строка 4 обычно содержит названия групп
        groups_row_idx = 4
        if groups_row_idx < len(current_df):
            groups = []
            for col_idx in range(2, min(6, len(current_df.columns))):
                val = str(current_df.iloc[groups_row_idx, col_idx]).strip()
                if val and val != "nan" and len(val) > 1:
                    # Нормализуем названия групп
                    if "до 50" in val.lower():
                        groups.append(("до 50 т", col_idx))
                    elif "от 50 до 80" in val.lower():
                        groups.append(("от 50 до 80 т", col_idx))
                    elif "свыше 100" in val.lower() or "св. 100" in val.lower():
                        groups.append(("свыше 100 т", col_idx))
                    else:
                        groups.append((val, col_idx))
        
        if not groups:
            groups = [("до 50 т", 2), ("от 50 до 80 т", 3), ("свыше 100 т", 4)]
        
        # Извлекаем код блока из исходной таблицы
        block_code = "БВ-2.3а"
        first_row = str(current_df.iloc[0, 0]) if len(current_df) > 0 else ""
        if "БВ-" in first_row:
            import re
            match = re.search(r"БВ-[\d.]+", first_row)
            if match:
                block_code = match.group()
        
        # Строим результат
        result_rows = []
        current_work_name = ""
        
        for row_idx in range(5, len(current_df)):
            row = current_df.iloc[row_idx]
            
            # Пропускаем служебные строки
            row_str = " ".join(str(v) for v in row.fillna("") if str(v).strip())
            if "Итого" in row_str or "норма входит" in row_str.lower():
                continue
            
            # Извлекаем номер и название работы
            number = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            work_name_cell = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
            
            full_work_name = ""
            
            if number and number != "nan" and number.isdigit():
                if work_name_cell and work_name_cell != "nan" and len(work_name_cell) > 2:
                    current_work_name = work_name_cell
                    full_work_name = work_name_cell
                else:
                    # Ищем название в предыдущих строках
                    for prev_idx in range(row_idx - 1, max(0, row_idx - 3), -1):
                        prev_cell = str(current_df.iloc[prev_idx, 1]).strip()
                        if prev_cell and prev_cell != "nan" and len(prev_cell) > 5:
                            current_work_name = prev_cell
                            full_work_name = prev_cell
                            break
            elif work_name_cell and work_name_cell != "nan" and len(work_name_cell) > 2:
                if current_work_name:
                    full_work_name = f"{current_work_name} {work_name_cell}"
                else:
                    full_work_name = work_name_cell
                    current_work_name = work_name_cell
            else:
                full_work_name = current_work_name if current_work_name else ""
            
            if not full_work_name or full_work_name == "nan":
                continue
            
            # Обрабатываем каждую группу
            for group_name, col_idx in groups:
                if col_idx >= len(row):
                    continue
                
                norm_value = row.iloc[col_idx]
                if pd.isna(norm_value) or str(norm_value).strip() == "":
                    continue
                
                try:
                    norm_float = float(norm_value)
                except (ValueError, TypeError):
                    continue
                
                # Создаем строку результата на основе структуры целевой таблицы
                result_row = {}
                
                # Определяем порядок колонок из целевой таблицы
                for col in target_columns:
                    if "Блок" in col or "КРС" in col:
                        result_row[col] = block_code
                    elif "Наименование" in col or "работ" in col:
                        result_row[col] = full_work_name.strip()
                    elif "Группы" in col or "агрегат" in col:
                        result_row[col] = group_name
                    elif "Норма" in col or "время" in col:
                        result_row[col] = norm_float
                
                result_rows.append(result_row)
        
        if result_rows:
            return pd.DataFrame(result_rows)
        else:
            return pd.DataFrame(columns=target_columns)

    def _convert_simple_pattern(
        self,
        current_df: pd.DataFrame,
        example_source: pd.DataFrame,
        example_target: pd.DataFrame,
    ) -> pd.DataFrame:
        """Преобразовать простую таблицу, сопоставляя колонки из примера."""
        # Простое сопоставление: находим похожие колонки и переименовываем
        result = current_df.copy()
        
        # Анализируем маппинг из примера
        source_cols = list(example_source.columns)
        target_cols = list(example_target.columns)
        
        # Пытаемся найти соответствия
        rename_map = {}
        for target_col in target_cols:
            # Ищем похожую колонку в исходной
            for source_col in source_cols:
                if source_col.lower() in target_col.lower() or target_col.lower() in source_col.lower():
                    if source_col in result.columns:
                        rename_map[source_col] = target_col
                        break
        
        if rename_map:
            result = result.rename(columns=rename_map)
        
        # Выбираем только те колонки, которые есть в целевом примере
        available_cols = [col for col in target_cols if col in result.columns]
        if available_cols:
            result = result[available_cols]
        
        return result

