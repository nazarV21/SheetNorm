"""Специальный конвертер для преобразования кросс-таблиц в плоскую структуру."""
from __future__ import annotations

from typing import Any
import pandas as pd
import re


def convert_cross_table_to_flat(df: pd.DataFrame, block_code: str = "БВ-2.3а") -> pd.DataFrame:
    """
    Преобразует кросс-таблицу в плоскую структуру.
    
    Ожидаемая структура исходной таблицы:
    - Строка 0-1: заголовок документа (можно пропустить)
    - Строка 2: заголовки "№ п.п.", "Наименование работ", "Норма времени, час"
    - Строка 3: подзаголовок "Группы подъемных агрегатов"
    - Строка 4: названия групп (например, "до 50 т", "от 50 до 80 т", "свыше 100 т")
    - Строка 5+: данные (номера, наименования работ, нормы времени)
    
    Результат:
    - Плоская таблица с колонками:
      - "Блок-вставки по КРС"
      - "Наименование работ"
      - "Группы подъемных агрегатов"
      - "Норма времени, час"
    """
    # Читаем данные без заголовков, если они еще не прочитаны
    if isinstance(df.columns, pd.RangeIndex):
        raw_df = df
    else:
        # Если заголовки уже применены, читаем заново без них
        raw_df = df.copy()
        raw_df.columns = range(len(raw_df.columns))
    
    # Находим строку с заголовками групп (обычно строка 4)
    groups_row_idx = 4
    
    if groups_row_idx >= len(raw_df):
        # Если структура отличается, пытаемся найти строку с группами
        for i in range(min(6, len(raw_df))):
            row_values = raw_df.iloc[i].fillna("").astype(str)
            if any("до 50 т" in str(v) or "от 50 до 80 т" in str(v) for v in row_values):
                groups_row_idx = i
                break
    
    # Извлекаем названия групп из строки groups_row_idx
    groups_row = raw_df.iloc[groups_row_idx]
    group_columns = {}
    
    # Ищем столбцы с группами (начинаются со строки 2, но значения в столбцах 2, 3, 4...)
    for col_idx in range(2, len(groups_row)):
        group_name = str(groups_row.iloc[col_idx]).strip()
        if group_name and group_name != "nan" and group_name != "NaN":
            # Очищаем название группы от лишних символов
            group_name = re.sub(r"\s+", " ", group_name)
            # Нормализуем названия групп
            if "до 50" in group_name.lower():
                group_name = "до 50 т"
            elif "от 50 до 80" in group_name.lower():
                group_name = "от 50 до 80 т"
            elif "свыше 100" in group_name.lower() or "св. 100" in group_name.lower():
                group_name = "свыше 100 т"
            
            if group_name and len(group_name) > 1:
                group_columns[col_idx] = group_name
    
    # Если не нашли группы в строке 4, ищем в заголовках
    if not group_columns:
        # Пробуем найти в заголовках исходной таблицы
        header_row = raw_df.iloc[3] if len(raw_df) > 3 else None
        if header_row is not None:
            for col_idx in range(len(header_row)):
                val = str(header_row.iloc[col_idx]).strip()
                if "до 50 т" in val or "от 50 до 80" in val:
                    # Ищем в следующей строке
                    if groups_row_idx + 1 < len(raw_df):
                        next_row = raw_df.iloc[groups_row_idx + 1]
                        for c in range(col_idx, min(col_idx + 5, len(next_row))):
                            g_name = str(next_row.iloc[c]).strip()
                            if g_name and g_name != "nan":
                                group_columns[c] = g_name
    
    # Стандартные названия групп, если не нашли автоматически
    if not group_columns:
        group_columns = {
            2: "до 50 т",
            3: "от 50 до 80 т",
            4: "свыше 100 т"
        }
    
    # Начинаем обработку данных со строки 5 (индекс 5)
    result_rows = []
    current_work_name = ""
    
    for row_idx in range(5, len(raw_df)):
        row = raw_df.iloc[row_idx]
        
        # Проверяем, не является ли это служебной строкой
        row_str = " ".join(str(v) for v in row.fillna("") if str(v).strip())
        if "Итого" in row_str or "норма входит" in row_str.lower():
            continue
        
        # Столбец 0 - номер (может быть NaN для подпунктов)
        number = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        
        # Столбец 1 - наименование работы
        work_name_cell = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        
        full_work_name = ""
        
        # Если есть номер - это новая основная работа
        if number and number != "nan" and number.isdigit():
            # Сохраняем полное название основной работы
            if work_name_cell and work_name_cell != "nan" and len(work_name_cell) > 2:
                current_work_name = work_name_cell
                full_work_name = work_name_cell
            else:
                # Ищем название в предыдущих строках
                for prev_idx in range(row_idx - 1, max(0, row_idx - 3), -1):
                    prev_cell = str(raw_df.iloc[prev_idx, 1]).strip()
                    if prev_cell and prev_cell != "nan" and len(prev_cell) > 5:
                        current_work_name = prev_cell
                        full_work_name = prev_cell
                        break
        
        # Если это подпункт (нет номера, но есть название в столбце 1)
        elif work_name_cell and work_name_cell != "nan" and len(work_name_cell) > 2:
            # Это подпункт - объединяем с основной работой
            if current_work_name:
                full_work_name = f"{current_work_name} {work_name_cell}"
            else:
                full_work_name = work_name_cell
                current_work_name = work_name_cell  # Сохраняем как основу для следующих подпунктов
        else:
            # Продолжаем использовать текущее название
            full_work_name = current_work_name if current_work_name else ""
        
        # Если нет названия работы, пропускаем строку
        if not full_work_name or full_work_name == "nan":
            continue
        
        # Обрабатываем каждую группу (столбцы с нормами времени)
        for col_idx, group_name in group_columns.items():
            if col_idx >= len(row):
                continue
                
            norm_value = row.iloc[col_idx]
            
            # Пропускаем пустые значения и NaN
            if pd.isna(norm_value) or str(norm_value).strip() == "" or str(norm_value).strip().lower() == "nan":
                continue
            
            # Пытаемся преобразовать в число
            try:
                norm_float = float(norm_value)
                if norm_float == 0.0 and col_idx == 4:  # Для последнего столбца иногда 0 означает отсутствие
                    continue
            except (ValueError, TypeError):
                continue
            
            # Создаём строку результата
            result_rows.append({
                "Блок-вставки по КРС": block_code,
                "Наименование работ": full_work_name.strip(),
                "Группы подъемных агрегатов": group_name,
                "Норма времени, час": norm_float
            })
    
    # Создаём итоговый DataFrame
    if result_rows:
        result_df = pd.DataFrame(result_rows)
        return result_df
    else:
        # Если ничего не получилось, возвращаем пустую таблицу с правильными колонками
        return pd.DataFrame(columns=[
            "Блок-вставки по КРС",
            "Наименование работ",
            "Группы подъемных агрегатов",
            "Норма времени, час"
        ])

