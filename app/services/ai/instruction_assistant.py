from __future__ import annotations

from pathlib import Path
from typing import Any
from difflib import SequenceMatcher
import json
import re

from flask import current_app

from app.services.table_structure_analyzer import TableStructureAnalyzer
from app.utils.feedback_store import FeedbackStore

from app.services.ai.model_manager import get_model_manager


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
    SPLIT_VERBS = ("разделить", "разнести", "разбить", "распределить", "разложить", "split", "separate")
    SHEET_WORDS = ("лист", "листы", "листов", "вкладк", "sheet", "sheets", "tabs")
    NEGATIVE_TOTAL_PATTERNS = (
        "не добавлять итог", "не добавляй итог", "не создавать итог", "не делай итог",
        "без итог", "итоги не нужны", "итог не нужен", "no total", "without total",
    )
    NEGATIVE_SUMMARY_PATTERNS = (
        "не создавать сводн", "не создавай сводн", "без сводн",
        "не создавать техническ", "не создавай техническ", "без техническ",
        "не создавать дополнительн", "не создавай дополнительн",
    )
    SPLIT_SHEET_PATTERNS = (
        "на разные листы", "на отдельные листы", "отдельный лист для каждого",
        "отдельные листы для", "отдельные листы по", "каждый на отдельный лист",
        "split by", "separate sheets by",
    )
    SUMMARY_SHEET_PATTERNS = (
        "сводный лист",
        "отдельный лист итогов",
        "лист с итогами",
        "общий лист итогов",
        "summary sheet",
    )

    def __init__(self) -> None:
        self.analyzer = TableStructureAnalyzer()
        manager = get_model_manager()
        self._model_manager = manager
        settings = manager.get_runtime_settings()
        self.backend = settings.backend
        self.context_tokens = settings.context_tokens
        self.max_completion_tokens = settings.max_completion_tokens
        self.temperature = settings.temperature
        # Loading a GGUF model is intentionally lazy. Merely opening the site,
        # previewing a workbook or running a non-AI conversion must not reserve
        # several gigabytes of RAM.
        self._llm = None

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

        # Автоанализ всегда получает только текущую инструкцию пользователя.
        # История исправлений не должна подменять новую задачу или незаметно
        # добавлять старые фильтры, итоги и названия колонок.
        analysis = self.analyzer.analyze_excel(excel_path, sheet_name=sheet_name, instruction_text=user_text)
        learning_examples: list[dict[str, Any]] = []
        if has_user_instruction and current_app.config.get("AI_USE_LEARNED_HINTS", False):
            learning_examples = FeedbackStore().find_relevant(
                user_text=user_text,
                fingerprint=(analysis.get("fingerprint") or {}),
                limit=3,
                require_text_overlap=True,
            )

        # Даже при включённом обучении история передаётся модели только как
        # структурная справка. Она не добавляется к user_text и не влияет на
        # определение пользовательского намерения.
        learned_hint = self._build_learned_instruction_hint(learning_examples) if learning_examples else ""
        if learned_hint:
            analysis["learned_instruction_hint"] = learned_hint

        first_intent = self._detect_user_intent(user_text, analysis)
        if has_user_instruction and self.backend == "llama_cpp":
            self._llm = self._model_manager.get_active_model()
            if self._llm is None:
                self.backend = "fallback"
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
            "Ты — помощник в системе нормализации Excel. Составь одну короткую и точную инструкцию для обработки файла.\n"
            "Абсолютный приоритет имеет ТЕКУЩАЯ ЗАДАЧА ПОЛЬЗОВАТЕЛЯ. Нельзя заменять её прошлым примером, "
            "добавлять операции, которых пользователь не просил, или повторять старые формулировки.\n"
            "Автоанализ помогает определить строки заголовков и структуру, но не меняет требуемый вид результата. "
            "Если пользователь просит отдельные листы, не заменяй это одним сводным листом или фильтром. "
            "Если пользователь просит итог для каждого листа, укажи итог внутри каждого листа.\n"
            "Пиши по-русски, без JSON, без вопросов, без рассуждений и без повторяющихся предложений. "
            "Сначала дословно сохрани смысл текущей задачи, затем добавь только необходимые технические детали файла.\n\n"
            f"АНАЛИЗ ТАБЛИЦЫ:\n{json.dumps(self._compact_analysis(analysis), ensure_ascii=False, indent=2)}\n\n"
            f"РАСПОЗНАННОЕ НАМЕРЕНИЕ:\n{json.dumps(user_intent, ensure_ascii=False, indent=2)}\n\n"
            f"НЕОБЯЗАТЕЛЬНЫЕ СТРУКТУРНЫЕ ПОДСКАЗКИ ИЗ ИСТОРИИ (не добавляй из них новые требования):\n"
            f"{json.dumps(self._compact_learning_examples(learning_examples or []), ensure_ascii=False, indent=2)}\n\n"
            f"ТЕКУЩАЯ ЗАДАЧА ПОЛЬЗОВАТЕЛЯ — СОХРАНИТЬ ЕЁ СМЫСЛ БЕЗ ИЗМЕНЕНИЙ:\n{user_text}\n\n"
            "ИТОГОВАЯ ИНСТРУКЦИЯ:"
        )
        try:
            prompt, max_tokens = self._fit_prompt_to_context(prompt, self.max_completion_tokens)
            completion = self._model_manager.create_completion(prompt=prompt, temperature=self.temperature, max_tokens=max_tokens)
            text = self._clean_text(completion["choices"][0]["text"].strip())
            if not self._instruction_respects_current_intent(text, user_text, user_intent):
                current_app.logger.warning("LLM instruction ignored because it does not preserve the current user intent")
                return None
            return text
        except Exception as exc:
            current_app.logger.warning("LLM instruction improvement failed", exc_info=exc)
            return None

    def _instruction_respects_current_intent(
        self,
        text: str,
        user_text: str,
        user_intent: dict[str, Any],
    ) -> bool:
        lower = (text or "").lower()
        if not lower:
            return False
        if user_intent.get("needs_split_sheets"):
            has_sheet = "лист" in lower or "sheet" in lower
            has_separation = any(word in lower for word in ("отдельн", "раздел", "разнес", "разб"))
            group_terms = list(user_intent.get("group_by_columns") or [])
            grouping_hint = str(user_intent.get("grouping_hint") or "")
            group_terms.append(grouping_hint)
            has_group = any(
                self._column_match_score(term, lower) >= 45
                for term in group_terms
                if str(term).strip()
            )
            if not (has_sheet and has_separation and has_group):
                return False
        if user_intent.get("needs_per_sheet_totals"):
            has_total = any(word in lower for word in ("итог", "сумм"))
            has_per_sheet = any(phrase in lower for phrase in ("каждого лист", "каждом лист", "для каждой группы", "по каждой группе"))
            if not (has_total and has_per_sheet):
                return False
        if user_intent.get("no_totals") and any(word in lower for word in ("добавить итог", "добавить строку итог", "сформировать итог")):
            return False
        user_tokens = {token for token in re.findall(r"[а-яёa-z0-9]{4,}", user_text.lower()) if token not in {"сделать", "нужно"}}
        text_tokens = set(re.findall(r"[а-яёa-z0-9]{4,}", lower))
        if user_tokens and len(user_tokens & text_tokens) < min(2, len(user_tokens)):
            return False
        return True

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
        parts: list[str] = []

        if user_text:
            parts.append("Основная задача пользователя: " + user_text.strip().rstrip(".!?") + ".")
        if draft:
            parts.append(draft)

        group_columns = list(user_intent.get("group_by_columns") or [])
        group_label = group_columns[0] if group_columns else str(user_intent.get("grouping_hint") or "указанной группировке")
        if user_intent.get("needs_split_sheets"):
            group_noun = self._group_genitive(str(user_intent.get("grouping_hint") or group_label))
            parts.append(
                f"Создать отдельный лист для каждого {group_noun}. "
                f"Разделить данные по колонке «{group_label}»; название листа должно совпадать со значением группировки."
            )
        if user_intent.get("fill_down_columns"):
            parts.append(
                "Заполнить пустые значения сверху вниз в колонках: "
                + ", ".join(f"«{name}»" for name in user_intent["fill_down_columns"])
                + "."
            )
        if user_intent.get("selected_columns"):
            parts.append(
                "В результате оставить колонки: "
                + ", ".join(f"«{name}»" for name in user_intent["selected_columns"])
                + "."
            )
        if user_intent.get("needs_group_filter") and not user_intent.get("needs_split_sheets"):
            parts.append(f"Включить автофильтр по колонке «{group_label}».")
        if user_intent.get("needs_per_sheet_totals"):
            group_noun = self._group_genitive(str(user_intent.get("grouping_hint") or group_label))
            parts.append(f"Внизу каждого листа {group_noun} добавить итог по числовым колонкам.")
        elif user_intent.get("needs_totals"):
            parts.append("Внизу итоговой таблицы добавить строку итогов по числовым колонкам.")
        if user_intent.get("needs_summary_sheet"):
            parts.append("Создать отдельный сводный лист с итогами по группам.")
        if user_intent.get("no_totals"):
            parts.append("Не добавлять итоговые строки.")
        if user_intent.get("no_summary_sheet"):
            parts.append("Не создавать сводные или технические листы.")
        return self._clean_text(" ".join(parts))

    def _detect_user_intent(self, user_text: str, analysis: dict[str, Any]) -> dict[str, Any]:
        text = self._normalize_match_text(user_text)
        all_headers = " ".join(self._all_analysis_columns(analysis)).lower()
        combined = f"{text} {all_headers}"

        split_requested = self._requests_split_sheets(text)
        grouping_hint = self._extract_grouping_hint(user_text) if split_requested else ""
        group_columns, ambiguous_columns = self._resolve_grouping_columns(analysis, grouping_hint)
        hierarchical_grouping = bool(
            split_requested
            and group_columns
            and self._needs_hierarchical_expansion(analysis, group_columns[0], grouping_hint)
        )

        no_totals = self._has_negated_totals(text)
        no_summary = self._has_negated_summary(text)
        totals_requested = any(word in text for word in self.TOTAL_WORDS) and not no_totals
        summary_requested = any(pattern in text for pattern in self.SUMMARY_SHEET_PATTERNS) and not no_summary
        filter_requested = any(word in text for word in self.FILTER_WORDS)

        selected_columns = self._extract_selected_columns(user_text, analysis)
        fill_down_columns = self._extract_fill_down_columns(user_text, analysis)
        if split_requested and not fill_down_columns:
            fill_down_columns = [
                col for col in (analysis.get("fill_down_candidates") or [])
                if col not in group_columns
            ]

        branch_requested = any(word in text for word in self.BRANCH_WORDS)
        sales_context = any(word in combined for word in self.SALES_WORDS)
        date_context = any(word in combined for word in self.DATE_WORDS)
        location_context = any(word in combined for word in self.WHERE_WORDS)

        return {
            "needs_split_sheets": bool(split_requested),
            "grouping_hint": grouping_hint,
            "group_by_columns": group_columns,
            "ambiguous_group_columns": ambiguous_columns,
            "hierarchical_grouping": hierarchical_grouping,
            "needs_group_filter": bool(filter_requested and group_columns and not split_requested),
            "needs_auto_filter": bool(filter_requested),
            "needs_totals": bool(totals_requested),
            "no_totals": bool(no_totals),
            "needs_summary_sheet": bool(summary_requested),
            "no_summary_sheet": bool(no_summary),
            "needs_per_sheet_totals": bool(split_requested and totals_requested),
            "selected_columns": selected_columns,
            "fill_down_columns": fill_down_columns,
            "keep_group_column": bool(group_columns and group_columns[0] in selected_columns),
            "sales_context": bool(sales_context),
            "date_context": bool(date_context),
            "location_context": bool(location_context),
            "subtotal_numeric_columns": bool(totals_requested),
            # Совместимость со старым интерфейсом и тестами.
            "needs_branch_filter": bool(branch_requested and filter_requested and not split_requested),
            "needs_summary_by_branch": bool(summary_requested),
            "needs_separate_sheets_by_branch": bool(split_requested),
            "likely_filter_columns": group_columns,
        }

    def _requests_split_sheets(self, text: str) -> bool:
        has_sheet = any(word in text for word in self.SHEET_WORDS)
        has_split = any(word in text for word in self.SPLIT_VERBS)
        has_each = bool(re.search(r"(?:для|по)\s+кажд\w+", text))
        explicit_pattern = any(pattern in text for pattern in self.SPLIT_SHEET_PATTERNS)
        return bool(has_sheet and (has_split or has_each or explicit_pattern))

    def _extract_grouping_hint(self, user_text: str) -> str:
        normalized = self._normalize_match_text(user_text)
        quoted_patterns = (
            r'(?:по|значениям)\s+колонк\w*\s*[«"\']([^»"\']+)[»"\']',
            r'колонк\w*\s*[«"\']([^»"\']+)[»"\']',
        )
        for pattern in quoted_patterns:
            match = re.search(pattern, user_text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()

        patterns = (
            r"(?:разделить|разнести|разбить|распределить|разложить)\s+(?:данные\s+)?по\s+([а-яёa-z0-9 _/-]{2,60}?)(?=\s+(?:и|на|в|для|создать|сделать)|[,.!?]|$)",
            r"(?:отдельн\w*\s+лист\w*|лист\w*\s+отдельно)\s+(?:для|по)\s+([а-яёa-z0-9 _/-]{2,60}?)(?=[,.!?]|$)",
            r"для\s+кажд\w+\s+([а-яёa-z0-9 _/-]{2,50}?)(?=\s+(?:создать|сделать|отдельн|лист)|[,.!?]|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                value = match.group(1).strip(" ,.-")
                value = re.sub(r"\b(?:уникальн\w*\s+значени\w*|колонк\w*)\b", "", value).strip()
                if value:
                    return value
        return ""

    def _resolve_grouping_columns(self, analysis: dict[str, Any], hint: str) -> tuple[list[str], list[str]]:
        columns = self._all_analysis_columns(analysis)
        if not hint or not columns:
            return [], []
        scored = sorted(
            ((self._column_match_score(hint, column), column) for column in columns),
            reverse=True,
        )
        if not scored or scored[0][0] < 48:
            return [], []
        top_score = scored[0][0]
        close = [column for score, column in scored if score >= max(48, top_score - 7)]
        if len(close) > 1 and top_score < 92:
            return [], close[:4]
        return [scored[0][1]], []

    def _extract_selected_columns(self, user_text: str, analysis: dict[str, Any]) -> list[str]:
        text = user_text or ""
        marker = re.search(r"(?:остав\w*|сохран\w*|вывест\w*)\s+колонк\w*", text, flags=re.IGNORECASE)
        if not marker:
            return []
        fragment = text[marker.start():]
        stop = re.search(r"\b(?:не\s+созда|не\s+добав|без\s+итог|итоги\s+не)\w*", fragment, flags=re.IGNORECASE)
        if stop:
            fragment = fragment[:stop.start()]
        quoted = self._quoted_values(fragment)
        if quoted:
            return self._resolve_column_list(analysis, quoted)
        fragment = re.split(r"колонк\w*", fragment, maxsplit=1, flags=re.IGNORECASE)[-1]
        values = [part.strip(" ,:«»\"\'") for part in re.split(r",|\s+и\s+", fragment) if part.strip()]
        return self._resolve_column_list(analysis, values)

    def _extract_fill_down_columns(self, user_text: str, analysis: dict[str, Any]) -> list[str]:
        sentences = re.split(r"[.;!?\n]+", user_text or "")
        for sentence in sentences:
            lower = sentence.lower()
            if not ("заполн" in lower and ("пуст" in lower or "сверху" in lower or "предыдущ" in lower)):
                continue
            quoted = self._quoted_values(sentence)
            if quoted:
                return self._resolve_column_list(analysis, quoted)
        return []

    def _resolve_column_list(self, analysis: dict[str, Any], values: list[str]) -> list[str]:
        columns = self._all_analysis_columns(analysis)
        result: list[str] = []
        for value in values:
            scored = sorted(
                ((self._column_match_score(value, column), column) for column in columns),
                reverse=True,
            )
            if scored and scored[0][0] >= 55 and scored[0][1] not in result:
                result.append(scored[0][1])
        return result

    def _needs_hierarchical_expansion(self, analysis: dict[str, Any], column: str, hint: str) -> bool:
        columns = self._all_analysis_columns(analysis)
        try:
            column_index = columns.index(column)
        except ValueError:
            return False
        rows = analysis.get("preview") or []
        data_start = int(analysis.get("data_start_row") or 0)
        hint_tokens = set(self._normalized_tokens(hint))
        markers = 0
        for row in rows[data_start:]:
            if column_index >= len(row):
                continue
            value = str(row[column_index] or "").strip()
            if not value:
                continue
            non_empty = sum(1 for cell in row if str(cell).strip())
            value_tokens = set(self._normalized_tokens(value))
            if non_empty == 1 and (not hint_tokens or hint_tokens & value_tokens):
                markers += 1
        return markers >= 2

    def _all_analysis_columns(self, analysis: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for item in (analysis.get("id_columns") or []) + (analysis.get("value_columns") or []):
            name = str(item.get("name") or "").strip()
            if name and name not in result:
                result.append(name)
        return result

    @staticmethod
    def _quoted_values(text: str) -> list[str]:
        values = re.findall(r'[«"\']([^»"\']+)[»"\']', text or '')
        return [value.strip() for value in values if value.strip()]

    def _column_match_score(self, requested: str, column: str) -> float:
        requested_norm = self._normalize_match_text(requested)
        column_norm = self._normalize_match_text(column)
        if not requested_norm or not column_norm:
            return 0.0
        if requested_norm == column_norm:
            return 100.0
        requested_tokens = set(self._normalized_tokens(requested_norm))
        column_tokens = set(self._normalized_tokens(column_norm))
        overlap = requested_tokens & column_tokens
        score = SequenceMatcher(None, requested_norm, column_norm).ratio() * 60
        if overlap:
            score = max(score, 75 + 20 * len(overlap) / max(len(requested_tokens), len(column_tokens), 1))
        if requested_norm in column_norm or column_norm in requested_norm:
            score = max(score, 88.0)
        return min(score, 100.0)

    @classmethod
    def _normalized_tokens(cls, text: str) -> list[str]:
        return [cls._stem_token(token) for token in re.findall(r"[а-яёa-z0-9]+", text.lower()) if token]

    @staticmethod
    def _stem_token(token: str) -> str:
        token = token.lower().replace("ё", "е")
        suffixes = (
            "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
            "ениям", "аниям", "ях", "ах", "ям", "ам", "ов", "ев", "ей",
            "ому", "ему", "ой", "ий", "ый", "ая", "яя", "ое", "ее",
            "ы", "и", "а", "я", "у", "ю", "е", "о",
        )
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) - len(suffix) >= 4:
                return token[:-len(suffix)]
        return token

    @staticmethod
    def _normalize_match_text(text: str) -> str:
        value = (text or "").lower().replace("ё", "е")
        value = re.sub(r"[^а-яa-z0-9 _/-]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _group_genitive(cls, value: str) -> str:
        tokens = cls._normalized_tokens(value)
        noun = tokens[0] if tokens else cls._normalize_match_text(value)
        known = {
            "филиал": "филиала",
            "склад": "склада",
            "регион": "региона",
            "город": "города",
            "офис": "офиса",
            "менеджер": "менеджера",
            "подразделен": "подразделения",
            "категор": "категории",
        }
        if noun in known:
            return known[noun]
        if noun.endswith(("й", "ь")):
            return noun[:-1] + "я"
        if noun.endswith("а"):
            return noun[:-1] + "ы"
        if noun.endswith("я"):
            return noun[:-1] + "и"
        return noun + "а"

    def _has_negated_totals(self, text: str) -> bool:
        return any(pattern in text for pattern in self.NEGATIVE_TOTAL_PATTERNS) or bool(
            re.search(r"не\s+(?:добав\w*|созда\w*|дела\w*)[^.!?]{0,40}итог", text)
        )

    def _has_negated_summary(self, text: str) -> bool:
        return any(pattern in text for pattern in self.NEGATIVE_SUMMARY_PATTERNS) or bool(
            re.search(r"не\s+(?:добав\w*|созда\w*|дела\w*)[^.!?]{0,50}(?:сводн|техническ)\w*\s+лист", text)
        )

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
        separate_sheets = bool(intent.get("needs_split_sheets"))
        group_columns = list(intent.get("group_by_columns") or [])
        selected_columns = list(intent.get("selected_columns") or [])
        fill_down_columns = list(intent.get("fill_down_columns") or [])

        if selected_columns:
            rule["select_columns"] = selected_columns
        if fill_down_columns:
            rule["fill_down_columns"] = fill_down_columns

        report_enabled = separate_sheets or any(
            intent.get(flag)
            for flag in ("needs_auto_filter", "needs_group_filter", "needs_totals", "needs_summary_sheet")
        )
        if not report_enabled:
            return rule

        hierarchical = bool(intent.get("hierarchical_grouping"))
        if separate_sheets and not group_columns:
            rule["planning_error"] = {
                "code": "GROUP_COLUMN_NOT_RESOLVED",
                "message": "Не удалось однозначно определить колонку для разделения по листам.",
                "candidates": list(intent.get("ambiguous_group_columns") or []),
            }
        elif separate_sheets and hierarchical:
            source_column = group_columns[0]
            if "/" in source_column:
                group_name, detail_name = [part.strip() for part in source_column.split("/", 1)]
            else:
                group_name = str(intent.get("grouping_hint") or source_column).strip().title()
                detail_name = source_column
            rule["hierarchical_groups"] = {
                "enabled": True,
                "source_column": source_column,
                "group_column_name": group_name,
                "detail_column_name": detail_name,
                "marker_words": self._normalized_tokens(intent.get("grouping_hint") or group_name),
                "remove_marker_rows": True,
                "require_empty_other_cells": True,
            }
            group_columns = [group_name]

        group_column = group_columns[0] if group_columns else None
        drop_group_column = bool(hierarchical and group_column not in selected_columns)
        rule["excel_report"] = {
            "enabled": True,
            "auto_filter": bool(intent.get("needs_auto_filter") or intent.get("needs_group_filter")),
            "freeze_header": True,
            "filter_columns": group_columns,
            "subtotal": {
                "enabled": bool(intent.get("needs_totals") and not separate_sheets),
                "numeric_columns": "auto",
                "label": "Итого",
            },
            "group_sheets": {
                "enabled": bool(separate_sheets and group_column),
                "group_by": group_columns,
                "drop_group_column": drop_group_column,
                "replace_result_sheet": True,
                "add_subtotal": bool(intent.get("needs_per_sheet_totals")),
                "subtotal_label": "Итого",
            },
            "summary_sheet": {
                "enabled": bool(intent.get("needs_summary_sheet")),
                "group_by": group_columns,
                "numeric_columns": "auto",
                "sheet_name": "Итоги",
            },
        }
        return rule

    def _build_understanding(self, analysis: dict[str, Any], user_text: str, intent: dict[str, Any]) -> list[dict[str, str]]:
        table_type_map = {
            "cross_table": "кросс-таблицу: часть значений расположена в колонках, поэтому возможен перевод в длинный формат",
            "multi_header": "таблицу с многоуровневыми заголовками",
            "flat": "плоскую таблицу",
        }
        overrides = analysis.get("instruction_overrides") or {}
        header_prefix = "по инструкции пользователя" if overrides.get("header_rows") or overrides.get("header_single_row") else "вероятные"
        data_prefix = "по инструкции пользователя" if overrides.get("data_start_row") is not None else "вероятно"
        items = [
            {"label": "Структура файла", "value": table_type_map.get(analysis.get("table_type"), str(analysis.get("table_type")))},
            {"label": "Заголовки", "value": f"{header_prefix} строки заголовков: {self._human_rows(analysis.get('header_rows') or [])}"},
            {"label": "Данные", "value": f"данные, {data_prefix}, начинаются со строки {int(analysis.get('data_start_row', 0)) + 1}"},
        ]
        if intent.get("needs_split_sheets"):
            group_columns = intent.get("group_by_columns") or []
            if group_columns:
                items.append({"label": "Разделение", "value": f"создать отдельный лист для каждого значения колонки «{group_columns[0]}»"})
            elif intent.get("ambiguous_group_columns"):
                items.append({"label": "Нужно уточнение", "value": "неоднозначная колонка группировки: " + ", ".join(intent["ambiguous_group_columns"])})
            else:
                items.append({"label": "Нужно уточнение", "value": "колонка для разделения по листам не найдена"})
        if intent.get("fill_down_columns"):
            items.append({"label": "Заполнение пропусков", "value": "заполнить сверху вниз: " + ", ".join(intent["fill_down_columns"])})
        if intent.get("selected_columns"):
            items.append({"label": "Колонки результата", "value": ", ".join(intent["selected_columns"])})
        if intent.get("needs_per_sheet_totals"):
            items.append({"label": "Итоги", "value": "добавить отдельный итог на каждом созданном листе"})
        elif intent.get("needs_totals"):
            items.append({"label": "Итоги", "value": "добавить итоговую строку по числовым колонкам"})
        elif intent.get("no_totals"):
            items.append({"label": "Итоги", "value": "не добавлять итоговые строки"})
        if intent.get("no_summary_sheet"):
            items.append({"label": "Дополнительные листы", "value": "не создавать сводные или технические листы"})
        if user_text:
            items.append({"label": "Текст пользователя", "value": "сохранён как исходная формулировка и привязан к реальным колонкам файла"})
        return items

    def _build_instruction_changes(
        self,
        raw: str,
        improved: str,
        analysis: dict[str, Any],
        intent: dict[str, Any],
    ) -> list[dict[str, str]]:
        changes: list[dict[str, str]] = []
        overrides = analysis.get("instruction_overrides") or {}
        if overrides.get("header_rows") or overrides.get("header_single_row"):
            changes.append({"type": "Учтено", "text": f"Строка заголовков: {self._human_rows(analysis.get('header_rows') or [])}."})
        if overrides.get("data_start_row") is not None:
            changes.append({"type": "Учтено", "text": f"Данные начинаются со строки {int(analysis.get('data_start_row', 0)) + 1}."})
        if intent.get("needs_split_sheets") and intent.get("group_by_columns"):
            changes.append({"type": "Учтено", "text": f"Данные будут разделены по колонке «{intent['group_by_columns'][0]}» на отдельные листы."})
        if intent.get("fill_down_columns"):
            changes.append({"type": "Добавлено", "text": "Заполнение пустых значений сверху вниз: " + ", ".join(intent["fill_down_columns"]) + "."})
        if intent.get("selected_columns"):
            changes.append({"type": "Учтено", "text": "Состав колонок результата: " + ", ".join(intent["selected_columns"]) + "."})
        if intent.get("needs_per_sheet_totals"):
            changes.append({"type": "Учтено", "text": "На каждом листе будет добавлен итог."})
        elif intent.get("no_totals"):
            changes.append({"type": "Учтено", "text": "Итоговые строки добавляться не будут."})
        if intent.get("no_summary_sheet"):
            changes.append({"type": "Учтено", "text": "Сводные и технические листы создаваться не будут."})
        if raw and self._clean_text(raw) != self._clean_text(improved):
            changes.append({"type": "Переформулировано", "text": "Короткая задача привязана к структуре и реальным колонкам загруженного файла."})
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
            "columns": [item.get("name") for item in (analysis.get("id_columns") or []) + (analysis.get("value_columns") or [])],
            "fill_down_candidates": analysis.get("fill_down_candidates") or [],
            "instruction_overrides": analysis.get("instruction_overrides") or {},
            "learned_instruction_hint": analysis.get("learned_instruction_hint"),
        }

    def _compact_learning_examples(self, examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Не передаём модели старые пользовательские инструкции: локальная LLM
        # склонна копировать их и тем самым подменять текущую задачу.
        compact: list[dict[str, Any]] = []
        for item in examples[:3]:
            structural_note = self._build_structural_learning_note(item)
            if structural_note:
                compact.append({
                    "similarity_score": item.get("similarity_score"),
                    "structural_note": structural_note,
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
        if (report.get("group_sheets") or {}).get("enabled"):
            summary.append("Для каждого филиала будет создан отдельный лист.")
            if (report.get("group_sheets") or {}).get("add_subtotal"):
                summary.append("На каждом листе филиала будет добавлен собственный итог.")
        if (report.get("summary_sheet") or {}).get("enabled"):
            summary.append("Будет создан отдельный сводный лист с итогами по филиалам/группам.")
        return summary or ["Правило будет пересобрано автоматически на основе инструкции и структуры файла."]


    @staticmethod
    def _clean_text(text: str) -> str:
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return text

        # Локальные модели иногда зацикливают последние служебные фразы.
        # Удаляем только точные повторы предложений, сохраняя первый экземпляр.
        sentences = re.split(r"(?<=[.!?])\s+", text)
        seen: set[str] = set()
        unique: list[str] = []
        for sentence in sentences:
            normalized = re.sub(r"[^a-zа-яё0-9]+", " ", sentence.lower()).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(sentence.strip())
        return " ".join(unique)
