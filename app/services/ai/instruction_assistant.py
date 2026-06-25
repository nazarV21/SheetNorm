from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

from flask import current_app

from app.services.table_structure_analyzer import TableStructureAnalyzer
from app.utils.feedback_store import FeedbackStore

try:
    from llama_cpp import Llama
except ImportError:  # optional dependency
    Llama = None  # type: ignore


class InstructionAssistant:
    """AI-помощник по составлению инструкции обработки Excel.

    Задача помощника — не только улучшить текст пользователя, но и показать,
    как система поняла файл и запрос: где видит заголовки, данные, служебные
    строки, нужны ли фильтры, итоги и отдельный лист с агрегированными итогами.

    Если локальная LLM подключена, она используется для переформулирования
    инструкции. Если модель недоступна, работает детерминированный fallback,
    чтобы интерфейс оставался полезным без .venv и без GGUF-файла модели.
    """

    BRANCH_WORDS = ("филиал", "подраздел", "регион", "город", "офис", "точка", "branch", "region", "office")
    TOTAL_WORDS = ("итог", "итого", "сумм", "посчит", "подсчит", "total", "sum")
    FILTER_WORDS = ("фильтр", "выбрать", "отобрать", "показать только", "filter")
    SALES_WORDS = ("продаж", "выруч", "реализац", "товар", "заказ", "sale", "revenue", "amount")
    DATE_WORDS = ("дата", "когда", "месяц", "период", "день", "год", "date", "period", "month")
    WHERE_WORDS = ("где", "адрес", "склад", "магазин", "точка", "место", "location")

    def __init__(self) -> None:
        self.analyzer = TableStructureAnalyzer()
        self.backend = current_app.config.get("AI_BACKEND", "llama_cpp")
        self.context_tokens = int(current_app.config.get("AI_CONTEXT_TOKENS", 8192))
        self.max_completion_tokens = int(current_app.config.get("AI_MAX_COMPLETION_TOKENS", 1000))
        self._llm = None
        if self.backend == "llama_cpp" and Llama:
            model_path = Path(current_app.config.get("AI_MODEL_PATH", ""))
            if model_path.exists():
                try:
                    self._llm = Llama(model_path=str(model_path), n_ctx=self.context_tokens, n_threads=6, verbose=False)
                except Exception as exc:
                    current_app.logger.warning("LLM model was found but could not be loaded: %s", exc)

    def prepare_instruction(
        self,
        excel_path: str | Path,
        user_text: str | None = None,
        sheet_name: str | int | None = None,
        previous_ai_instruction: str | None = None,
    ) -> dict[str, Any]:
        """Проанализировать Excel и собрать пользовательскую инструкцию.

        Важно: пользователь не редактирует JSON-правило. При каждом изменении
        текстовой инструкции этот метод вызывается заново: файл повторно
        анализируется, намерение пользователя определяется заново, а внутреннее
        формализованное правило пересобирается автоматически.
        """
        user_text = (user_text or "").strip()
        previous_ai_instruction = (previous_ai_instruction or "").strip()
        has_user_instruction = bool(user_text)

        # Первый анализ учитывает только текущую инструкцию пользователя.
        analysis = self.analyzer.analyze_excel(excel_path, sheet_name=sheet_name, instruction_text=user_text)
        learning_examples = []
        if has_user_instruction:
            learning_examples = FeedbackStore().find_relevant(
                user_text=user_text,
                fingerprint=(analysis.get("fingerprint") or {}),
                limit=5,
            )

        # Если для похожих файлов уже была исправленная инструкция, используем её
        # как скрытую подсказку только вместе с явной задачей пользователя. Для
        # пустого поля инструкции это слишком агрессивно: система начинает
        # угадывать цель обработки и может уверенно построить неверный preview.
        learned_hint = self._build_learned_instruction_hint(learning_examples) if has_user_instruction else ""
        if learned_hint and learned_hint.lower() not in user_text.lower():
            analysis = self.analyzer.analyze_excel(
                excel_path,
                sheet_name=sheet_name,
                instruction_text=(user_text + "\n" + learned_hint).strip(),
            )
            analysis["learned_instruction_hint"] = learned_hint

        first_intent = self._detect_user_intent(user_text, analysis)
        improved = self._improve_with_llm(analysis, user_text, first_intent, learning_examples) if self._llm and has_user_instruction else None
        if not improved:
            improved = (
                self._fallback_improve(analysis, user_text, first_intent, learning_examples)
                if has_user_instruction
                else self._analysis_only_instruction(analysis)
            )

        # Report actions must come from the current user text, not from learned hints
        # or the improved instruction, otherwise filters/totals leak into similar files.
        final_intent = first_intent
        how_understood = self._build_understanding(analysis, improved or user_text, final_intent)

        rule = dict(analysis.get("proposed_rule") or {})
        rule = self._apply_user_intent_to_rule(rule, final_intent)
        rule["source"] = "instruction_assistant"
        rule["instruction"] = improved
        rule["user_intent"] = final_intent

        return {
            "analysis": analysis,
            "user_raw_prompt": user_text,
            "previous_ai_instruction": previous_ai_instruction,
            "ai_improved_instruction": improved,
            "generated_rule": rule,
            "engine": "analysis_only" if not has_user_instruction else (self.backend if self._llm else "fallback"),
            "requires_user_instruction": not has_user_instruction,
            "ready_for_preview": has_user_instruction,
            "user_intent": final_intent,
            "how_ai_understood": how_understood,
            "instruction_changes": self._build_instruction_changes(user_text, improved, analysis, final_intent),
            "learning_examples": self._compact_learning_examples(learning_examples),
            "internal_rule_summary": self._build_internal_rule_summary(rule),
            "clarifying_questions": analysis.get("questions") or [],
            "suggested_user_instructions": self._build_suggested_user_instructions(analysis),
            "instruction_overrides": analysis.get("instruction_overrides") or {},
            "learned_instruction_hint": analysis.get("learned_instruction_hint"),
        }

    def _analysis_only_instruction(self, analysis: dict[str, Any]) -> str:
        sheet = analysis.get("selected_sheet") or "первый лист"
        headers = self._human_rows(analysis.get("header_rows") or [])
        data_start = analysis.get("data_start_row_human") or "не определено"
        table_type = analysis.get("table_type") or "unknown"
        return (
            f"Файл проанализирован без пользовательской задачи. Лист: {sheet}. "
            f"Предположительный тип таблицы: {table_type}. "
            f"Предположительные строки заголовков: {headers}; данные начинаются со строки {data_start}. "
            "Перед построением результата уточните, что нужно получить: какие строки удалить, какие колонки оставить, "
            "нужно ли разворачивать таблицу в длинный формат, какие фильтры или итоги добавить."
        )

    def _build_suggested_user_instructions(self, analysis: dict[str, Any]) -> list[str]:
        suggestions = [
            "Если разметка верная: очистить таблицу, удалить пустые строки и привести заголовки к нормальному виду.",
            f"Если заголовки определены неверно: заголовки на строке {analysis.get('data_start_row_human') or 1}, данные со следующей строки.",
        ]
        if analysis.get("total_rows"):
            suggestions.append("Удалить строки Итого, Всего и Total из результата.")
        if analysis.get("table_type") == "cross_table":
            suggestions.append("Месяцы или периоды в колонках перенести в колонку Период, значения перенести в колонку Значение.")
        elif analysis.get("table_type") == "multi_header":
            suggestions.append("Объединить многоуровневые заголовки в понятные названия колонок без разворота в длинный формат.")
        id_columns = [str(item.get("name")) for item in (analysis.get("id_columns") or [])[:4] if item.get("name")]
        if id_columns:
            suggestions.append("Оставить ключевые колонки: " + ", ".join(id_columns) + ".")
        return suggestions[:6]

    def _improve_with_llm(
        self,
        analysis: dict[str, Any],
        user_text: str,
        user_intent: dict[str, Any],
        learning_examples: list[dict[str, Any]] | None = None,
    ) -> str | None:
        prompt = (
            "Ты — помощник в системе нормализации Excel. Пользователь может описывать задачу неточно.\n"
            "На основе анализа таблицы, намерения пользователя, прошлых исправлений и текста пользователя составь точную инструкцию для преобразования.\n"
            "Критически важно: если пользователь явно указал строку заголовков, одну строку заголовков, строку начала данных или запретил длинный формат, НЕ переопределяй это автоанализом. "
            "Пиши по-русски, без JSON и без технических ключей. Инструкция должна быть понятна аналитику. Не задавай вопросов в итоговой инструкции; если что-то неясно, используй безопасное предположение и отдельно система покажет подсказки для уточнения. "
            "Обязательно укажи: где заголовки, где начинаются данные, что удалить, нужно ли расплавление, "
            "какие фильтры и итоги нужно добавить в Excel. Если прошлые исправления показывают ошибку, не повторяй её.\n\n"
            f"АНАЛИЗ ТАБЛИЦЫ:\n{json.dumps(self._compact_analysis(analysis), ensure_ascii=False, indent=2)}\n\n"
            f"РАСПОЗНАННОЕ НАМЕРЕНИЕ ПОЛЬЗОВАТЕЛЯ:\n{json.dumps(user_intent, ensure_ascii=False, indent=2)}\n\n"
            f"ТЕКСТ ПОЛЬЗОВАТЕЛЯ:\n{user_text or 'Пользователь пока не дал инструкцию.'}\n\n"
            f"ПРОШЛЫЕ ИСПРАВЛЕНИЯ ПОЛЬЗОВАТЕЛЕЙ, КОТОРЫЕ НУЖНО УЧЕСТЬ:\n"
            f"{json.dumps(self._compact_learning_examples(learning_examples or []), ensure_ascii=False, indent=2)}\n\n"
            "ИТОГОВАЯ ИНСТРУКЦИЯ:"
        )
        try:
            prompt, max_tokens = self._fit_prompt_to_context(prompt, self.max_completion_tokens)
            completion = self._llm.create_completion(prompt=prompt, temperature=0.15, max_tokens=max_tokens)
            text = completion["choices"][0]["text"].strip()
            return self._clean_text(text)
        except Exception as exc:
            current_app.logger.warning("LLM instruction improvement failed", exc_info=exc)
            return None

    def _fit_prompt_to_context(self, prompt: str, requested_max_tokens: int) -> tuple[str, int]:
        token_count = self._count_tokens(prompt)
        reserved_tokens = 128
        available_for_response = self.context_tokens - token_count - reserved_tokens
        if available_for_response >= requested_max_tokens:
            return prompt, requested_max_tokens
        if available_for_response >= 128:
            return prompt, available_for_response

        prompt_budget = max(512, self.context_tokens - max(requested_max_tokens, 256) - reserved_tokens)
        compact_prompt = self._truncate_prompt(prompt, prompt_budget)
        compact_token_count = self._count_tokens(compact_prompt)
        max_tokens = self.context_tokens - compact_token_count - reserved_tokens
        if max_tokens < 128:
            raise ValueError(
                f"LLM prompt is too large after compaction: {compact_token_count} tokens for context {self.context_tokens}"
            )
        current_app.logger.warning(
            "LLM prompt was compacted to fit context window: original_tokens=%s compact_tokens=%s context_tokens=%s",
            token_count,
            compact_token_count,
            self.context_tokens,
        )
        return compact_prompt, min(requested_max_tokens, max_tokens)

    def _count_tokens(self, prompt: str) -> int:
        if self._llm is not None and hasattr(self._llm, "tokenize"):
            return len(self._llm.tokenize(prompt.encode("utf-8"), add_bos=True))
        return max(1, len(prompt) // 4)

    def _truncate_prompt(self, prompt: str, token_budget: int) -> str:
        if self._count_tokens(prompt) <= token_budget:
            return prompt
        marker = "\n\n[Контекст сокращён из-за лимита окна локальной LLM.]\n\n"
        ratio = token_budget / max(self._count_tokens(prompt), 1)
        char_budget = max(1000, int(len(prompt) * ratio * 0.85))
        head_chars = int(char_budget * 0.6)
        tail_chars = max(500, char_budget - head_chars - len(marker))
        compact = prompt[:head_chars] + marker + prompt[-tail_chars:]
        while self._count_tokens(compact) > token_budget and head_chars > 500 and tail_chars > 300:
            head_chars = int(head_chars * 0.85)
            tail_chars = int(tail_chars * 0.85)
            compact = prompt[:head_chars] + marker + prompt[-tail_chars:]
        return compact

    def _fallback_improve(
        self,
        analysis: dict[str, Any],
        user_text: str,
        user_intent: dict[str, Any],
        learning_examples: list[dict[str, Any]] | None = None,
    ) -> str:
        draft = analysis.get("draft_instruction", "")
        questions = analysis.get("questions", [])
        parts = [draft]

        if user_text:
            parts.append("Дополнительное требование пользователя: " + user_text.strip())

        if user_intent.get("needs_branch_filter"):
            parts.append(
                "В итоговом Excel-файле нужно включить автофильтр по колонке филиала/подразделения, "
                "чтобы пользователь мог выбрать конкретный филиал и увидеть только относящиеся к нему строки."
            )
        if user_intent.get("needs_totals"):
            parts.append(
                "Внизу таблицы нужно добавить строку итогов по числовым показателям. "
                "Итоги должны быть рассчитаны формулами SUBTOTAL, чтобы при применении фильтра по филиалу "
                "пересчитывались только видимые строки."
            )
        if user_intent.get("needs_summary_by_branch"):
            parts.append(
                "Также нужно сформировать отдельный лист 'Итоги' с суммарными продажами/значениями по каждому филиалу."
            )
        if user_intent.get("sales_context"):
            parts.append(
                "Для контекста продаж желательно сохранить признаки 'что продалось', 'где продалось' и 'когда продалось', "
                "если такие колонки присутствуют в исходном файле."
            )
        learning_examples = learning_examples or []
        if learning_examples:
            last = learning_examples[0]
            learned_structure = self._build_structural_learning_note(last)
            changed_terms = last.get("changed_terms") or []
            preview_columns = last.get("preview_columns") or last.get("corrected_columns") or []
            if learned_structure:
                parts.append(
                    "С учётом прошлой правки для похожей таблицы не повторять прежнюю ошибку и учесть только структуру обработки: "
                    + learned_structure
                )
            if changed_terms:
                parts.append(
                    "В прошлой правке пользователь добавлял важные уточнения: "
                    + ", ".join(str(term) for term in changed_terms[:10])
                    + "."
                )
            if preview_columns:
                parts.append(
                    "В похожих случаях в результате ожидались такие колонки: "
                    + ", ".join(str(col) for col in preview_columns[:12])
                    + "."
                )
        # Уточняющие вопросы больше не вшиваются в итоговую инструкцию: пользователь
        # отвечает на них через редактирование инструкции, а не через отдельный JSON или ручную правку таблицы.
        return self._clean_text(" ".join(parts))

    def _detect_user_intent(self, user_text: str, analysis: dict[str, Any]) -> dict[str, Any]:
        text = (user_text or "").lower()
        all_headers = " ".join(
            str(col.get("name", ""))
            for col in (analysis.get("id_columns") or []) + (analysis.get("value_columns") or [])
        ).lower()
        combined = f"{text} {all_headers}"

        branch_requested = any(word in text for word in self.BRANCH_WORDS)
        branch_available = branch_requested or any(word in all_headers for word in self.BRANCH_WORDS)
        totals_requested = any(word in text for word in self.TOTAL_WORDS)
        filter_requested = any(word in text for word in self.FILTER_WORDS)
        summary_requested = any(word in text for word in ("лист", "отдельн", "свод", "summary", "sheet"))
        sales_context = any(word in combined for word in self.SALES_WORDS)
        date_context = any(word in combined for word in self.DATE_WORDS)
        location_context = any(word in combined for word in self.WHERE_WORDS)

        likely_filter_columns = self._find_likely_columns(analysis, self.BRANCH_WORDS)
        if branch_available and not likely_filter_columns:
            likely_filter_columns = ["Филиал"]

        return {
            "needs_branch_filter": bool(branch_requested and filter_requested),
            "needs_auto_filter": bool(filter_requested),
            "needs_totals": bool(totals_requested),
            "needs_summary_by_branch": bool(totals_requested and summary_requested),
            "sales_context": bool(sales_context),
            "date_context": bool(date_context),
            "location_context": bool(location_context),
            "likely_filter_columns": likely_filter_columns,
            "subtotal_numeric_columns": bool(totals_requested),
        }

    def _find_likely_columns(self, analysis: dict[str, Any], words: tuple[str, ...]) -> list[str]:
        columns = (analysis.get("id_columns") or []) + (analysis.get("value_columns") or [])
        result: list[str] = []
        for col in columns:
            name = str(col.get("name", ""))
            lower = name.lower()
            if any(word in lower for word in words):
                result.append(name)
        return result[:3]

    def _apply_user_intent_to_rule(self, rule: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        report_enabled = any(
            intent.get(flag)
            for flag in ("needs_auto_filter", "needs_branch_filter", "needs_totals", "needs_summary_by_branch")
        )
        if not report_enabled:
            return rule
        rule["excel_report"] = {
            "enabled": True,
            "auto_filter": bool(intent.get("needs_auto_filter") or intent.get("needs_branch_filter")),
            "freeze_header": True,
            "filter_columns": intent.get("likely_filter_columns") or [],
            "subtotal": {
                "enabled": bool(intent.get("needs_totals")),
                "numeric_columns": "auto",
                "label": "Итого по выбранному фильтру",
            },
            "summary_sheet": {
                "enabled": bool(intent.get("needs_summary_by_branch")),
                "group_by": intent.get("likely_filter_columns") or ["Филиал"],
                "numeric_columns": "auto",
                "sheet_name": "Итоги",
            },
        }
        return rule

    def _build_understanding(self, analysis: dict[str, Any], user_text: str, intent: dict[str, Any]) -> list[dict[str, str]]:
        table_type_map = {
            "cross_table": "кросс-таблицу: часть значений расположена в колонках, поэтому возможен перевод в длинный формат",
            "multi_header": "таблицу с многоуровневыми заголовками: несколько строк нужно объединить в названия колонок",
            "flat": "плоскую таблицу: её можно очищать и оформлять без сложного расплавления",
        }
        overrides = analysis.get("instruction_overrides") or {}
        header_prefix = "по инструкции пользователя" if overrides.get("header_rows") or overrides.get("header_single_row") else "вероятные"
        data_prefix = "по инструкции пользователя" if overrides.get("data_start_row") is not None else "вероятно"
        items = [
            {"label": "Структура файла", "value": table_type_map.get(analysis.get("table_type"), str(analysis.get("table_type")))},
            {"label": "Заголовки", "value": f"{header_prefix} строки заголовков: {self._human_rows(analysis.get('header_rows') or [])}"},
            {"label": "Данные", "value": f"данные, {data_prefix}, начинаются со строки {int(analysis.get('data_start_row', 0)) + 1}"},
        ]
        if overrides:
            items.append({"label": "Приоритет инструкции", "value": "явные указания пользователя применены поверх автоматического анализа файла"})
        if analysis.get("merged_ranges_count"):
            items.append({"label": "Объединённые ячейки", "value": f"обнаружено диапазонов: {analysis.get('merged_ranges_count')}"})
        if intent.get("needs_branch_filter"):
            cols = ", ".join(intent.get("likely_filter_columns") or ["Филиал"])
            items.append({"label": "Фильтр", "value": f"нужно добавить автофильтр по филиалу/подразделению: {cols}"})
        if intent.get("needs_totals"):
            items.append({"label": "Итоги", "value": "нужно добавить итоговую строку по числовым колонкам с формулами SUBTOTAL"})
        if intent.get("needs_summary_by_branch"):
            items.append({"label": "Сводный лист", "value": "нужно создать отдельный лист с итогами по каждому филиалу"})
        if user_text:
            items.append({"label": "Текст пользователя", "value": "сохранён как исходная формулировка, но дополнен техническими деталями"})
        return items

    def _build_instruction_changes(
        self,
        raw: str,
        improved: str,
        analysis: dict[str, Any],
        intent: dict[str, Any],
    ) -> list[dict[str, str]]:
        changes: list[dict[str, str]] = []
        if not raw:
            changes.append({"type": "Добавлено", "text": "Пользователь не ввёл подробную инструкцию, поэтому система собрала её из анализа структуры файла."})
        overrides = analysis.get("instruction_overrides") or {}
        if overrides.get("header_rows") or overrides.get("header_single_row"):
            changes.append({"type": "Учтено", "text": f"Применено указание пользователя по заголовкам: {self._human_rows(analysis.get('header_rows') or [])}."})
        else:
            changes.append({"type": "Уточнено", "text": f"Добавлены строки заголовков: {self._human_rows(analysis.get('header_rows') or [])}."})
        if overrides.get("data_start_row") is not None:
            changes.append({"type": "Учтено", "text": f"Применено указание пользователя по началу данных: строка {int(analysis.get('data_start_row', 0)) + 1}."})
        else:
            changes.append({"type": "Уточнено", "text": f"Добавлено предполагаемое начало данных: строка {int(analysis.get('data_start_row', 0)) + 1}."})
        if analysis.get("table_type") == "cross_table":
            changes.append({"type": "Добавлено", "text": "Указано, что таблицу можно преобразовать из широкого формата в длинный формат."})
        if intent.get("needs_branch_filter"):
            changes.append({"type": "Добавлено", "text": "Добавлено требование включить фильтр по филиалу/подразделению в итоговом Excel."})
        if intent.get("needs_totals"):
            changes.append({"type": "Добавлено", "text": "Добавлено требование создать итоговую строку, пересчитывающуюся после применения фильтра."})
        if intent.get("needs_summary_by_branch"):
            changes.append({"type": "Добавлено", "text": "Добавлено требование сформировать отдельный лист с итогами по филиалам."})
        if raw and self._clean_text(raw) != self._clean_text(improved):
            changes.append({"type": "Переформулировано", "text": "Разговорная формулировка преобразована в техническую инструкцию для правила обработки."})
        return changes

    @staticmethod
    def _human_rows(rows: list[int]) -> str:
        return ", ".join(str(int(row) + 1) for row in rows) if rows else "не определены"

    def _build_learned_instruction_hint(self, examples: list[dict[str, Any]]) -> str:
        """Собрать скрытую подсказку из прошлых исправлений для похожего файла."""
        if not examples:
            return ""
        strong = [item for item in examples if int(item.get("similarity_score") or 0) >= 8]
        if not strong:
            return ""
        parts: list[str] = []
        for item in strong[:2]:
            learned_structure = self._build_structural_learning_note(item)
            if learned_structure:
                parts.append(learned_structure)
        if not parts:
            return ""
        return "Учитывая прошлые исправления для похожих файлов, применить только структурные подсказки: " + " ".join(parts)

    def _build_structural_learning_note(self, item: dict[str, Any]) -> str:
        rule = item.get("generated_rule") or {}
        pieces: list[str] = []
        if rule.get("header_rows"):
            pieces.append(f"строки заголовков {self._human_rows(rule.get('header_rows') or [])}")
        if rule.get("data_start_row") is not None:
            pieces.append(f"данные начинаются со строки {int(rule.get('data_start_row')) + 1}")
        if rule.get("table_type"):
            pieces.append(f"тип таблицы {rule.get('table_type')}")
        melt = rule.get("melt") or {}
        if melt.get("enabled"):
            pieces.append("нужно преобразование из широкого формата в строки")
        elif rule:
            pieces.append("без дополнительного разворота, если текущий запрос этого не просит")
        return "; ".join(pieces)

    @staticmethod
    def _compact_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
        return {
            "file_name": analysis.get("file_name"),
            "selected_sheet": analysis.get("selected_sheet"),
            "table_type": analysis.get("table_type"),
            "header_rows": analysis.get("header_rows"),
            "data_start_row": analysis.get("data_start_row"),
            "id_columns": analysis.get("id_columns"),
            "value_columns": analysis.get("value_columns")[:12] if analysis.get("value_columns") else [],
            "merged_ranges_count": analysis.get("merged_ranges_count"),
            "total_rows": analysis.get("total_rows"),
            "questions": analysis.get("questions"),
            "instruction_overrides": analysis.get("instruction_overrides") or {},
            "learned_instruction_hint": analysis.get("learned_instruction_hint"),
        }

    @staticmethod
    def _compact_learning_examples(examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in examples[:5]:
            compact.append({
                "similarity_score": item.get("similarity_score"),
                "raw_prompt": item.get("raw_prompt"),
                "previous_ai_instruction": item.get("previous_ai_instruction") or item.get("ai_instruction"),
                "user_corrected_instruction": item.get("user_corrected_instruction") or item.get("user_instruction"),
                "regenerated_instruction": item.get("regenerated_instruction"),
                "changed_terms": item.get("changed_terms") or [],
                "preview_columns": item.get("preview_columns") or item.get("corrected_columns") or [],
                "action": item.get("action"),
            })
        return compact

    def _build_internal_rule_summary(self, rule: dict[str, Any]) -> list[str]:
        """Короткое человекочитаемое описание внутреннего правила без JSON."""
        summary: list[str] = []
        if rule.get("table_type"):
            summary.append(f"Тип обработки: {rule.get('table_type')}.")
        overrides = rule.get("instruction_overrides") or {}
        if rule.get("header_rows"):
            if overrides.get("header_rows") or overrides.get("header_single_row"):
                summary.append(f"Строки заголовков взяты из инструкции пользователя: {self._human_rows(rule.get('header_rows') or [])}.")
            else:
                summary.append(f"Строки заголовков определены автоматически: {self._human_rows(rule.get('header_rows') or [])}.")
        if rule.get("data_start_row") is not None:
            summary.append(f"Данные будут взяты начиная со строки {int(rule.get('data_start_row')) + 1}.")
        melt = rule.get("melt") or {}
        if melt.get("enabled") or rule.get("table_type") == "cross_table":
            summary.append("Таблица будет приведена из широкого формата к плоскому/длинному формату.")
        report = rule.get("excel_report") or {}
        if report.get("auto_filter"):
            summary.append("В итоговом Excel будет включён автофильтр.")
        if (report.get("subtotal") or {}).get("enabled"):
            summary.append("В итоговом Excel будет добавлена строка итогов с пересчётом после фильтра.")
        if (report.get("summary_sheet") or {}).get("enabled"):
            summary.append("Будет создан отдельный лист с итогами по филиалам/группам.")
        return summary or ["Правило будет пересобрано автоматически на основе инструкции и структуры файла."]


    @staticmethod
    def _clean_text(text: str) -> str:
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        text = re.sub(r"\s+", " ", text).strip()
        return text
