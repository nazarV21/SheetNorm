from __future__ import annotations

from pathlib import Path
from typing import Any
import json
from datetime import datetime

import pandas as pd
from flask import current_app
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from app.services.ai.llm_client import AIClient
from app.services.ai.smart_converter import SmartConverter
from app.utils.file_manager import FileManager
from app.utils.rules_store import RulesStore
from app.utils.training_examples_store import TrainingExamplesStore
from app.utils.cross_table_converter import convert_cross_table_to_flat


class ConversionValidationError(Exception):
    """Ошибка, которую нужно показать пользователю на этапе предпросмотра/конвертации."""

    def __init__(self, message: str, diagnostics: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.diagnostics = diagnostics or []


class ConversionService:
    """Основные шаги конвертации Excel-файла.

    Поддерживает три сценария:
    1. без правила — сохранить таблицу как есть;
    2. с сохранённым правилом/шаблоном;
    3. с разовой инструкцией пользователя, улучшенной AI-помощником.
    """

    def __init__(self) -> None:
        self.file_manager = FileManager(
            input_dir=current_app.config["INPUT_DIR"],
            output_dir=current_app.config["OUTPUT_DIR"],
        )
        self.ai_client = AIClient()
        self.smart_converter = SmartConverter()

    def analyze(self, job_id: str) -> dict[str, Any]:
        try:
            sample_df = self._load_sample(job_id)
        except FileNotFoundError:
            return {"error": "Файл не найден", "job_id": job_id}
        suggestions = self.ai_client.suggest_schema(sample_df)
        return {"job_id": job_id, "suggestions": suggestions}

    def convert_with_rule(self, job_id: str, rule_id: str | None, options: dict[str, Any]) -> dict[str, Any]:
        source_path = self.file_manager.resolve_input(job_id)
        if not source_path.exists():
            return {"error": "Файл не найден", "job_id": job_id}

        rule = None
        if rule_id:
            rule = RulesStore().get_rule(rule_id)
            if not rule:
                return {"error": "Выбранное правило не найдено", "job_id": job_id}

        result_df = self._convert_path(source_path=source_path, rule=rule, options=options)
        return self._finish(job_id, source_path, result_df, rule)

    def _build_instruction_rule(
        self,
        instruction: str,
        generated_rule: dict[str, Any] | None,
        name: str = "Разовая инструкция",
    ) -> dict[str, Any]:
        return {
            "id": None,
            "name": name,
            "prompt": instruction,
            "ai_improved_instruction": instruction,
            "generated_rule": generated_rule or {},
            "use_raw_data": True,
            "sheet_name": (generated_rule or {}).get("sheet_name"),
        }

    def convert_with_instruction(
        self,
        job_id: str,
        instruction: str,
        generated_rule: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Конвертация по разовой инструкции без обязательного сохранения правила."""
        source_path = self.file_manager.resolve_input(job_id)
        if not source_path.exists():
            return {"error": "Файл не найден", "job_id": job_id}
        rule = self._build_instruction_rule(instruction, generated_rule, name="Разовая инструкция")
        result_df = self._convert_path(source_path=source_path, rule=rule, options=options or {})
        return self._finish(job_id, source_path, result_df, rule)

    def convert_with_instruction_checked(
        self,
        job_id: str,
        instruction: str,
        generated_rule: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Финальная конвертация после предпросмотра.

        В отличие от старого режима, метод не молча возвращает сырой DataFrame при ошибке.
        Если внутреннее правило не применилось или результат выглядит подозрительно,
        пользователь получает понятное сообщение и должен уточнить инструкцию.
        """
        source_path = self.file_manager.resolve_input(job_id)
        if not source_path.exists():
            return {"error": "Файл не найден", "job_id": job_id, "diagnostics": []}
        rule = self._build_instruction_rule(instruction, generated_rule, name="Разовая инструкция")
        try:
            result_df, diagnostics = self._convert_path_with_diagnostics(
                source_path=source_path,
                rule=rule,
                options=options or {},
                strict=True,
            )
        except ConversionValidationError as exc:
            return {"error": exc.message, "job_id": job_id, "diagnostics": exc.diagnostics}
        result = self._finish(job_id, source_path, result_df, rule)
        result["diagnostics"] = diagnostics
        return result

    def preview_with_instruction(
        self,
        job_id: str,
        instruction: str,
        generated_rule: dict[str, Any] | None = None,
        max_rows: int = 100,
    ) -> dict[str, Any]:
        """Сформировать предпросмотр результата без финального скачивания.

        Если на этапе применения правила возникли ошибки, система не отдаёт
        случайный сырой результат, а возвращает диагностику для правки инструкции.
        """
        source_path = self.file_manager.resolve_input(job_id)
        if not source_path.exists():
            return {"error": "Файл не найден", "job_id": job_id, "diagnostics": []}
        rule = self._build_instruction_rule(instruction, generated_rule, name="Предпросмотр по инструкции")
        try:
            df, diagnostics = self._convert_path_with_diagnostics(
                source_path=source_path,
                rule=rule,
                options={},
                strict=True,
            )
        except ConversionValidationError as exc:
            return {"error": exc.message, "job_id": job_id, "diagnostics": exc.diagnostics}
        preview = self.dataframe_to_preview(df, max_rows=max_rows)
        preview["total_rows"] = int(len(df))
        preview["total_columns"] = int(len(df.columns))
        return {
            "job_id": job_id,
            "source_filename": self.file_manager.get_original_filename(job_id),
            "dataframe": df,
            "preview": preview,
            "diagnostics": diagnostics,
        }

    def save_corrected_dataframe(
        self,
        job_id: str,
        df: pd.DataFrame,
        rule: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Сохранить пользовательски исправленную итоговую таблицу как Excel."""
        source_path = self.file_manager.resolve_input(job_id)
        if not source_path.exists():
            return {"error": "Файл не найден", "job_id": job_id}
        return self._finish(job_id, source_path, df, rule)

    @staticmethod
    def dataframe_to_preview(df: pd.DataFrame, max_rows: int = 100) -> dict[str, Any]:
        """Подготовить безопасные данные для HTML-предпросмотра итоговой таблицы."""
        limited = df.head(max_rows).copy()
        limited = limited.where(pd.notna(limited), "")
        columns = [str(col) for col in limited.columns]
        rows: list[list[str]] = []
        for _, row in limited.iterrows():
            rows.append([str(value) if value is not None else "" for value in row.tolist()])
        return {
            "columns": columns,
            "rows": rows,
            "shown_rows": len(rows),
            "is_limited": len(df) > max_rows,
        }


    @staticmethod
    def dataframe_to_editable_preview(df: pd.DataFrame, max_rows: int = 100) -> dict[str, Any]:
        """Совместимость со старым патчем: теперь это обычный предпросмотр без редактирования."""
        return ConversionService.dataframe_to_preview(df, max_rows=max_rows)

    @staticmethod
    def dataframe_from_editable_payload(payload: dict[str, Any]) -> pd.DataFrame:
        """Собрать DataFrame из таблицы, которую пользователь поправил в браузере."""
        columns = [str(col).strip() or f"Колонка {idx + 1}" for idx, col in enumerate(payload.get("columns") or [])]
        raw_rows = payload.get("rows") or []
        normalized_rows: list[list[Any]] = []
        for raw_row in raw_rows:
            row = list(raw_row) if isinstance(raw_row, list) else []
            if len(row) < len(columns):
                row.extend([""] * (len(columns) - len(row)))
            elif len(row) > len(columns):
                row = row[: len(columns)]
            # Не сохраняем полностью пустые строки, которые пользователь мог случайно добавить.
            if any(str(value).strip() for value in row):
                normalized_rows.append(row)
        return pd.DataFrame(normalized_rows, columns=columns)

    def convert(self, job_id: str, schema: dict | None, options: dict[str, Any]) -> dict[str, Any]:
        source_path = self.file_manager.resolve_input(job_id)
        if not source_path.exists():
            return {"error": "Файл не найден", "job_id": job_id}
        df = pd.read_excel(source_path)
        if schema:
            df = self._apply_schema(df, schema)
        return self._finish(job_id, source_path, df, None)

    def _convert_path(self, source_path: Path, rule: dict[str, Any] | None, options: dict[str, Any]) -> pd.DataFrame:
        df, _ = self._convert_path_with_diagnostics(source_path, rule, options, strict=False)
        return df

    def _convert_path_with_diagnostics(
        self,
        source_path: Path,
        rule: dict[str, Any] | None,
        options: dict[str, Any],
        strict: bool = False,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        diagnostics: list[dict[str, Any]] = []

        def add(severity: str, stage: str, message: str, hint: str | None = None) -> None:
            item = {"severity": severity, "stage": stage, "message": message}
            if hint:
                item["hint"] = hint
            diagnostics.append(item)

        if not rule:
            try:
                df = pd.read_excel(source_path)
                diagnostics.extend(self._validate_converted_dataframe(df, stage="read_excel"))
                return df, diagnostics
            except Exception as exc:
                add("error", "read_excel", f"Не удалось прочитать Excel-файл: {exc}", "Проверьте, что файл не повреждён и имеет формат .xlsx/.xls.")
                raise ConversionValidationError("Файл не удалось прочитать как Excel.", diagnostics)

        prompt = rule.get("ai_improved_instruction") or rule.get("prompt") or ""
        generated_rule = rule.get("generated_rule") or {}
        sheet_name = rule.get("sheet_name") or generated_rule.get("sheet_name") or 0
        use_raw_data = bool(rule.get("use_raw_data") or generated_rule)

        # 1. Формализованное правило имеет приоритет. В строгом режиме ошибка правила
        # не должна маскироваться возвратом сырой таблицы: пользователь должен увидеть,
        # что нужно уточнить инструкцию.
        if generated_rule:
            try:
                df_rule = self._apply_generated_rule(source_path, generated_rule)
                validation = self._validate_converted_dataframe(df_rule, stage="generated_rule")
                diagnostics.extend(validation)
                if any(item.get("severity") == "error" for item in validation) and strict:
                    raise ConversionValidationError(
                        "Результат применения инструкции выглядит некорректно. Уточните инструкцию и обновите предпросмотр.",
                        diagnostics,
                    )
                add("info", "generated_rule", "Внутреннее правило успешно применено к Excel-файлу.")
                return df_rule, diagnostics
            except ConversionValidationError:
                raise
            except Exception as exc:
                add(
                    "error",
                    "generated_rule",
                    f"Ошибка применения внутреннего правила: {exc}",
                    "Уточните в инструкции строку заголовков, строку начала данных, нужные колонки и необходимость преобразования в длинный формат.",
                )
                if strict:
                    raise ConversionValidationError(
                        "Внутреннее правило не применилось. Исправьте инструкцию и обновите предпросмотр.",
                        diagnostics,
                    )

        # 2. Базовое чтение Excel. Оно используется только как источник для few-shot/LLM,
        # но в строгом режиме не считается полноценным успешным результатом, если до этого
        # было сформировано правило и оно упало.
        try:
            df = pd.read_excel(source_path)
        except Exception:
            df = pd.read_excel(source_path, sheet_name=0)

        if rule.get("id"):
            training_examples = TrainingExamplesStore().list_examples(rule_id=rule.get("id"))
        else:
            training_examples = TrainingExamplesStore().list_examples()

        if training_examples and prompt:
            try:
                if use_raw_data:
                    raw_df = self._read_raw(source_path, sheet_name)
                    df_smart = self.smart_converter.convert_with_examples(df, training_examples, prompt, raw_data=raw_df)
                else:
                    df_smart = self.smart_converter.convert_with_examples(df, training_examples, prompt)
                if df_smart is not None and len(df_smart.columns) > 0:
                    diagnostics.extend(self._validate_converted_dataframe(df_smart, stage="few_shot"))
                    add("info", "few_shot", "Результат построен с учётом обучающих примеров.")
                    return df_smart, diagnostics
            except Exception as exc:
                add("warning", "few_shot", f"Не удалось применить обучающие примеры: {exc}")

        if prompt:
            try:
                if use_raw_data:
                    raw_df = self._read_raw(source_path, sheet_name)
                    df_ai = self.ai_client.apply_prompt(df, prompt, raw_data=raw_df, training_examples=training_examples)
                else:
                    df_ai = self.ai_client.apply_prompt(df, prompt, training_examples=training_examples)
                if df_ai is not None and len(df_ai.columns) > 0:
                    diagnostics.extend(self._validate_converted_dataframe(df_ai, stage="llm_prompt"))
                    add("info", "llm_prompt", "Результат построен по текстовой инструкции.")
                    return df_ai, diagnostics
            except Exception as exc:
                add("warning", "llm_prompt", f"LLM не смогла применить текстовую инструкцию: {exc}")

        if rule.get("type") == "cross_table":
            try:
                block_code = rule.get("block_code", "БВ-2.3а")
                raw_df = self._read_raw(source_path, sheet_name)
                df_cross = convert_cross_table_to_flat(raw_df, block_code=block_code)
                diagnostics.extend(self._validate_converted_dataframe(df_cross, stage="cross_table_fallback"))
                add("info", "cross_table_fallback", "Применён резервный алгоритм для кросс-таблиц.")
                return df_cross, diagnostics
            except Exception as exc:
                add("warning", "cross_table_fallback", f"Резервный алгоритм кросс-таблиц не сработал: {exc}")

        fallback_diagnostics = self._validate_converted_dataframe(df, stage="raw_fallback")
        diagnostics.extend(fallback_diagnostics)
        add(
            "warning",
            "raw_fallback",
            "Система не смогла уверенно применить инструкцию и получила результат обычным чтением Excel.",
            "Уточните инструкцию: где заголовки, где начинаются данные, какие строки удалить и какие итоги нужны.",
        )
        if strict:
            raise ConversionValidationError(
                "Конвертация требует уточнения инструкции. Система не должна отдавать сырой результат как готовый файл.",
                diagnostics,
            )
        return df, diagnostics

    def _validate_converted_dataframe(self, df: pd.DataFrame, stage: str) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        if df is None:
            return [{"severity": "error", "stage": stage, "message": "Преобразование не вернуло таблицу."}]
        if len(df.columns) == 0:
            diagnostics.append({"severity": "error", "stage": stage, "message": "В результате нет колонок."})
        if len(df) == 0:
            diagnostics.append({"severity": "error", "stage": stage, "message": "В результате нет строк данных."})

        columns = [str(col).strip() for col in df.columns]
        generic = [col for col in columns if not col or col.lower().startswith("unnamed") or col.lower().startswith("колонка")]
        if columns and len(generic) / max(len(columns), 1) >= 0.5:
            diagnostics.append({
                "severity": "error",
                "stage": stage,
                "message": "Большая часть заголовков не распознана.",
                "hint": "Укажите в инструкции точную строку заголовков: например, 'заголовки находятся на строке 4, данные начинаются со строки 5'.",
            })
        elif generic:
            diagnostics.append({
                "severity": "warning",
                "stage": stage,
                "message": f"Есть нераспознанные заголовки: {', '.join(generic[:5])}.",
                "hint": "При необходимости уточните, какие колонки нужно оставить и как они должны называться.",
            })

        duplicates = sorted({col for col in columns if columns.count(col) > 1})
        if duplicates:
            diagnostics.append({
                "severity": "warning",
                "stage": stage,
                "message": f"Обнаружены повторяющиеся названия колонок: {', '.join(duplicates[:5])}.",
                "hint": "Уточните в инструкции, нужно ли объединять заголовки или переименовать одинаковые колонки.",
            })
        return diagnostics

    def _apply_generated_rule(self, source_path: Path, rule: dict[str, Any]) -> pd.DataFrame:
        sheet_name = rule.get("sheet_name") or 0
        raw_df = self._read_raw(source_path, sheet_name)
        table_type = rule.get("table_type", "flat")
        header_rows = [int(i) for i in (rule.get("header_rows") or [0])]
        data_start_row = int(rule.get("data_start_row") if rule.get("data_start_row") is not None else (max(header_rows) + 1))
        drop_contains = [str(x).lower() for x in (rule.get("drop_rows_contains") or [])]

        col_count = raw_df.shape[1]
        headers = self._make_headers(raw_df, header_rows, col_count)
        data = raw_df.iloc[data_start_row:].copy()
        data = data.dropna(how="all")
        data.columns = headers[: data.shape[1]]
        data = data.reset_index(drop=True)

        if drop_contains:
            mask = pd.Series(False, index=data.index)
            for marker in drop_contains:
                row_text = data.astype(str).agg(" ".join, axis=1).str.lower()
                mask = mask | row_text.str.contains(marker, regex=False, na=False)
            data = data.loc[~mask].reset_index(drop=True)

        data = self._drop_empty_columns(data)
        data = self._normalize_column_names(data)

        melt = rule.get("melt") or {}
        if table_type == "cross_table" or melt.get("enabled"):
            id_columns = [c.get("name") for c in (rule.get("id_columns") or []) if c.get("name")]
            value_columns = [c.get("name") for c in (rule.get("value_columns") or []) if c.get("name")]
            id_columns = [self._normalize_header(c) for c in id_columns if self._normalize_header(c) in data.columns]
            value_columns = [self._normalize_header(c) for c in value_columns if self._normalize_header(c) in data.columns]

            if not value_columns:
                id_set = set(id_columns)
                value_columns = [col for col in data.columns if col not in id_set and self._is_value_like_series(data[col])]
            if not id_columns:
                id_columns = [col for col in data.columns if col not in value_columns][:2]
            if value_columns:
                data = pd.melt(
                    data,
                    id_vars=id_columns,
                    value_vars=value_columns,
                    var_name=melt.get("var_name", "Период"),
                    value_name=melt.get("value_name", "Значение"),
                )
                data = data.dropna(subset=[melt.get("value_name", "Значение")], how="all")
                data = data.reset_index(drop=True)

        return data

    def _read_raw(self, source_path: Path, sheet_name: str | int | None) -> pd.DataFrame:
        try:
            return pd.read_excel(source_path, sheet_name=sheet_name if sheet_name not in (None, "") else 0, header=None)
        except Exception:
            return pd.read_excel(source_path, sheet_name=0, header=None)

    def _make_headers(self, raw_df: pd.DataFrame, header_rows: list[int], col_count: int) -> list[str]:
        headers: list[str] = []
        for col_idx in range(col_count):
            parts: list[str] = []
            for row_idx in header_rows:
                if row_idx >= raw_df.shape[0] or col_idx >= raw_df.shape[1]:
                    continue
                value = raw_df.iat[row_idx, col_idx]
                if pd.isna(value):
                    continue
                text = str(value).strip()
                if text and text.lower() != "nan":
                    parts.append(text)
            header = " / ".join(dict.fromkeys(parts)) if parts else f"Колонка {col_idx + 1}"
            headers.append(self._normalize_header(header))
        return headers

    @staticmethod
    def _normalize_header(text: Any) -> str:
        value = str(text).strip().replace("\n", " ")
        value = " ".join(value.split())
        return value or "Без названия"

    def _normalize_column_names(self, df: pd.DataFrame) -> pd.DataFrame:
        new_cols: list[str] = []
        seen: dict[str, int] = {}
        for col in df.columns:
            base = self._normalize_header(col)
            count = seen.get(base, 0)
            seen[base] = count + 1
            new_cols.append(base if count == 0 else f"{base}_{count + 1}")
        df.columns = new_cols
        return df

    @staticmethod
    def _drop_empty_columns(df: pd.DataFrame) -> pd.DataFrame:
        return df.dropna(axis=1, how="all")

    @staticmethod
    def _is_value_like_series(series: pd.Series) -> bool:
        non_empty = series.dropna()
        if non_empty.empty:
            return False
        converted = pd.to_numeric(non_empty.astype(str).str.replace(" ", "", regex=False).str.replace(",", ".", regex=False), errors="coerce")
        return converted.notna().mean() >= 0.5

    def _load_sample(self, job_id: str, max_rows: int = 50) -> pd.DataFrame:
        path = self.file_manager.resolve_input(job_id)
        return pd.read_excel(path, nrows=max_rows)

    def _apply_schema(self, df: pd.DataFrame, schema: dict) -> pd.DataFrame:
        rename_map = schema.get("rename", {})
        df = df.rename(columns=rename_map)
        select_cols = schema.get("columns") or schema.get("select_columns")
        if select_cols:
            df = df[[col for col in select_cols if col in df.columns]]
        calculated = schema.get("calculated", [])
        for rule in calculated:
            target = rule.get("name")
            expr = rule.get("expr") or rule.get("formula")
            if target and expr:
                try:
                    df[target] = df.eval(expr)
                except Exception:
                    pass
        return df

    def _save_dataframe(self, df: pd.DataFrame, source_path: Path, job_id: str, rule: dict[str, Any] | None = None) -> Path:
        output_path = self.file_manager.prepare_output(job_id, extension=".xlsx")
        df.to_excel(output_path, index=False, engine="openpyxl", sheet_name="Результат")
        self._format_excel_report(output_path, df, rule)
        return output_path

    def _format_excel_report(self, output_path: Path, df: pd.DataFrame, rule: dict[str, Any] | None) -> None:
        """Добавляет в итоговый Excel удобный отчётный слой.

        Если правило содержит excel_report, файл получает:
        - автофильтр по таблице, чтобы пользователь мог выбрать филиал;
        - закрепление строки заголовков;
        - итоговую строку с SUBTOTAL, которая пересчитывается после фильтра;
        - отдельный лист "Итоги" с агрегированными значениями по филиалам/подразделениям.
        """
        generated_rule = (rule or {}).get("generated_rule") or {}
        report = generated_rule.get("excel_report") or (rule or {}).get("excel_report") or {}
        if not report or not report.get("enabled"):
            return

        try:
            wb = load_workbook(output_path)
            ws = wb.active
            ws.title = "Результат"
            max_row = ws.max_row
            max_col = ws.max_column
            if max_row < 1 or max_col < 1:
                wb.save(output_path)
                return

            header_fill = PatternFill("solid", fgColor="EAF2FF")
            total_fill = PatternFill("solid", fgColor="FFF7D6")
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = header_fill
                cell.alignment = Alignment(wrap_text=True, vertical="center")
            ws.freeze_panes = "A2" if report.get("freeze_header", True) else None

            data_last_row = max_row
            if report.get("auto_filter", True) and max_row >= 2:
                ws.auto_filter.ref = f"A1:{get_column_letter(max_col)}{data_last_row}"

            for idx, column_name in enumerate(df.columns, start=1):
                width = min(max(12, len(str(column_name)) + 2), 42)
                ws.column_dimensions[get_column_letter(idx)].width = width

            subtotal_cfg = report.get("subtotal") or {}
            numeric_columns = self._detect_numeric_columns(df)
            if subtotal_cfg.get("enabled") and numeric_columns and max_row >= 2:
                total_row = max_row + 2
                label_col = 1
                ws.cell(total_row, label_col).value = subtotal_cfg.get("label") or "Итого по выбранному фильтру"
                ws.cell(total_row, label_col).font = Font(bold=True)
                ws.cell(total_row, label_col).fill = total_fill
                for col_idx in numeric_columns:
                    col_letter = get_column_letter(col_idx + 1)
                    cell = ws.cell(total_row, col_idx + 1)
                    cell.value = f"=SUBTOTAL(109,{col_letter}2:{col_letter}{max_row})"
                    cell.font = Font(bold=True)
                    cell.fill = total_fill
                for col_idx in range(1, max_col + 1):
                    ws.cell(total_row, col_idx).fill = total_fill

            summary_cfg = report.get("summary_sheet") or {}
            if summary_cfg.get("enabled"):
                self._add_summary_sheet(wb, df, summary_cfg)

            wb.save(output_path)
        except Exception:
            # Форматирование отчёта не должно ломать саму конвертацию.
            return

    def _add_summary_sheet(self, wb, df: pd.DataFrame, summary_cfg: dict[str, Any]) -> None:
        group_by = summary_cfg.get("group_by") or []
        group_columns = self._match_existing_columns(df, group_by)
        if not group_columns:
            group_columns = self._match_existing_columns(df, ["Филиал", "Подразделение", "Регион", "Город", "Branch", "Region"])
        numeric_indexes = self._detect_numeric_columns(df)
        numeric_columns = [df.columns[idx] for idx in numeric_indexes]
        if not group_columns or not numeric_columns:
            return

        work_df = df.copy()
        for col in numeric_columns:
            work_df[col] = pd.to_numeric(
                work_df[col].astype(str).str.replace(" ", "", regex=False).str.replace(",", ".", regex=False),
                errors="coerce",
            )
        summary = work_df.groupby(group_columns, dropna=False)[numeric_columns].sum(min_count=1).reset_index()

        sheet_name = str(summary_cfg.get("sheet_name") or "Итоги")[:31]
        if sheet_name in wb.sheetnames:
            del wb[sheet_name]
        ws = wb.create_sheet(sheet_name)
        for col_idx, col_name in enumerate(summary.columns, start=1):
            cell = ws.cell(1, col_idx, str(col_name))
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="EAF2FF")
        for row_idx, row in enumerate(summary.itertuples(index=False), start=2):
            for col_idx, value in enumerate(row, start=1):
                ws.cell(row_idx, col_idx, value)
        if summary.shape[0] >= 1:
            ws.auto_filter.ref = f"A1:{get_column_letter(summary.shape[1])}{summary.shape[0] + 1}"
        ws.freeze_panes = "A2"
        for idx, column_name in enumerate(summary.columns, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = min(max(14, len(str(column_name)) + 2), 42)

    def _detect_numeric_columns(self, df: pd.DataFrame) -> list[int]:
        numeric_columns: list[int] = []
        for idx, col in enumerate(df.columns):
            series = df[col].dropna()
            if series.empty:
                continue
            converted = pd.to_numeric(
                series.astype(str).str.replace(" ", "", regex=False).str.replace(",", ".", regex=False),
                errors="coerce",
            )
            if converted.notna().mean() >= 0.6:
                numeric_columns.append(idx)
        return numeric_columns

    def _match_existing_columns(self, df: pd.DataFrame, names: list[str]) -> list[str]:
        result: list[str] = []
        lowered = {str(col).lower(): col for col in df.columns}
        for wanted in names:
            wanted_lower = str(wanted).lower()
            for lower, original in lowered.items():
                if wanted_lower and (wanted_lower == lower or wanted_lower in lower or lower in wanted_lower):
                    if original not in result:
                        result.append(original)
        return result

    def _finish(self, job_id: str, source_path: Path, df: pd.DataFrame, rule: dict[str, Any] | None) -> dict[str, Any]:
        output_path = self._save_dataframe(df, source_path, job_id, rule=rule)
        original_name = self.file_manager.get_original_filename(job_id)
        result = {
            "job_id": job_id,
            "source_filename": original_name,
            "rows": len(df),
            "columns": len(df.columns),
            "output": str(output_path),
            "output_filename": Path(output_path).name,
            "status": "converted",
            "rule_id": rule.get("id") if rule else None,
            "rule_name": rule.get("name") if rule else None,
        }
        self._append_history_entry(result)
        return result

    def _append_history_entry(self, result: dict[str, Any]) -> None:
        history_path = Path(current_app.config["HISTORY_FILE"])
        try:
            existing: list[dict[str, Any]] = json.loads(history_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            existing = []
        except json.JSONDecodeError:
            existing = []

        entry = {
            "job_id": result["job_id"],
            "source_filename": result.get("source_filename"),
            "output_filename": result.get("output_filename"),
            "rows": result.get("rows"),
            "columns": result.get("columns"),
            "status": result.get("status"),
            "rule_name": result.get("rule_name"),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        existing.append(entry)
        history_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
