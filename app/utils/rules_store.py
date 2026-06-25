from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import uuid
from datetime import datetime

from flask import current_app
from pydantic import ValidationError

from app.services.rule_schema import normalize_declarative_rule


VALID_TABLE_TYPES = {"flat", "multi_header", "cross_table"}
VALID_OPERATIONS = {
    "drop_rows",
    "rename_columns",
    "select_columns",
    "melt",
    "fill_merged",
    "drop_empty",
    "filter_rows",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RulesStore:
    """Хранилище правил/шаблонов конвертации в JSON-файле.

    Правило теперь хранит не только промпт, но и:
    - исходный текст пользователя;
    - улучшенную инструкцию AI-помощника;
    - формализованное JSON-правило;
    - fingerprint структуры типовой таблицы.
    Это позволяет переиспользовать промпты для типовых таблиц.
    """

    def __init__(self) -> None:
        self.path = Path(current_app.config["RULES_FILE"])

    def _load_all(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            return []

    def _save_all(self, rules: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_rules(self) -> list[dict[str, Any]]:
        rules = self._load_all()
        for rule in rules:
            rule.setdefault("type", "prompt_template")
            rule.setdefault("prompt", rule.get("ai_improved_instruction") or rule.get("description") or "")
            rule.setdefault("description", "")
            rule.setdefault("domain", "universal")
            rule.setdefault("category", rule.get("domain") or "universal")
            rule.setdefault("table_type", (rule.get("generated_rule") or {}).get("table_type") or (rule.get("fingerprint") or {}).get("table_type") or "flat")
            rule.setdefault("version", 1)
            rule.setdefault("tags", [])
        return rules

    @staticmethod
    def validate_rule_payload(
        *,
        name: str,
        prompt: str,
        generated_rule: dict[str, Any] | None,
        table_type: str | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        generated_rule = generated_rule or {}
        if not name.strip():
            warnings.append("Не указано название шаблона.")
        if not prompt.strip():
            warnings.append("Не указана инструкция обработки.")

        resolved_type = table_type or generated_rule.get("table_type")
        if resolved_type and resolved_type not in VALID_TABLE_TYPES:
            warnings.append(f"Неизвестный тип таблицы: {resolved_type}.")

        header_rows = generated_rule.get("header_rows") or []
        if any(not isinstance(value, int) or value < 0 for value in header_rows):
            warnings.append("Строки заголовков должны быть неотрицательными целыми числами.")
        data_start = generated_rule.get("data_start_row")
        if data_start is not None and (not isinstance(data_start, int) or data_start < 0):
            warnings.append("Строка начала данных должна быть неотрицательным целым числом.")
        if header_rows and isinstance(data_start, int) and data_start <= max(header_rows):
            warnings.append("Строка начала данных должна находиться после строк заголовков.")

        operations = generated_rule.get("operations") or []
        for operation in operations:
            name_value = operation.get("type") if isinstance(operation, dict) else operation
            if name_value not in VALID_OPERATIONS:
                warnings.append(f"Неизвестная операция в JSON-правиле: {name_value}.")
        return warnings

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        for rule in self.list_rules():
            if rule.get("id") == rule_id:
                return rule
        return None

    def add_rule(
        self,
        name: str,
        prompt: str,
        *,
        raw_prompt: str | None = None,
        generated_rule: dict[str, Any] | None = None,
        fingerprint: dict[str, Any] | None = None,
        description: str | None = None,
        domain: str | None = None,
        use_raw_data: bool | None = None,
        sheet_name: str | None = None,
        category: str | None = None,
        table_type: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Добавить новое правило/шаблон с промптом и метаданными."""
        rules = self._load_all()
        try:
            generated_rule = normalize_declarative_rule(generated_rule or {})
            validation_errors: list[str] = []
        except ValidationError as exc:
            validation_errors = [f"JSON rule validation: {error['msg']}" for error in exc.errors()]
            generated_rule = generated_rule or {}
        resolved_table_type = table_type or (generated_rule or {}).get("table_type") or (fingerprint or {}).get("table_type") or "flat"
        warnings = self.validate_rule_payload(
            name=name,
            prompt=prompt,
            generated_rule=generated_rule,
            table_type=resolved_table_type,
        )
        warnings.extend(validation_errors)
        rule = {
            "id": str(uuid.uuid4()),
            "type": "prompt_template",
            "name": name,
            "description": description or "",
            "domain": domain or "universal",
            "category": category or domain or "universal",
            "table_type": resolved_table_type,
            "raw_user_prompt": raw_prompt or prompt,
            "ai_improved_instruction": prompt,
            "prompt": prompt,
            "generated_rule": generated_rule or {},
            "fingerprint": fingerprint or {},
            "use_raw_data": bool(use_raw_data) if use_raw_data is not None else bool(generated_rule),
            "sheet_name": sheet_name or (generated_rule or {}).get("sheet_name"),
            "created_at": _now(),
            "updated_at": _now(),
            "version": 1,
            "tags": tags or [],
            "validation_warnings": warnings,
        }
        rules.append(rule)
        self._save_all(rules)
        return rule

    def update_rule(
        self,
        rule_id: str,
        name: str | None = None,
        prompt: str | None = None,
        use_raw_data: bool | None = None,
        sheet_name: str | None = None,
        raw_user_prompt: str | None = None,
        generated_rule: dict[str, Any] | None = None,
        fingerprint: dict[str, Any] | None = None,
        description: str | None = None,
        domain: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Обновить правило."""
        rules = self._load_all()
        for rule in rules:
            if rule.get("id") == rule_id:
                if name is not None:
                    rule["name"] = name
                if prompt is not None:
                    rule["prompt"] = prompt
                    rule["ai_improved_instruction"] = prompt
                if raw_user_prompt is not None:
                    rule["raw_user_prompt"] = raw_user_prompt
                if generated_rule is not None:
                    try:
                        rule["generated_rule"] = normalize_declarative_rule(generated_rule)
                    except ValidationError:
                        rule["generated_rule"] = generated_rule
                if fingerprint is not None:
                    rule["fingerprint"] = fingerprint
                if description is not None:
                    rule["description"] = description
                if domain is not None:
                    rule["domain"] = domain
                if use_raw_data is not None:
                    rule["use_raw_data"] = bool(use_raw_data)
                if sheet_name is not None:
                    if sheet_name:
                        rule["sheet_name"] = sheet_name
                    else:
                        rule.pop("sheet_name", None)
                for key, value in kwargs.items():
                    if value is not None:
                        rule[key] = value
                    else:
                        rule.pop(key, None)
                rule["updated_at"] = _now()
                rule["version"] = int(rule.get("version") or 1) + 1
                rule["validation_warnings"] = self.validate_rule_payload(
                    name=str(rule.get("name") or ""),
                    prompt=str(rule.get("prompt") or ""),
                    generated_rule=rule.get("generated_rule") or {},
                    table_type=rule.get("table_type"),
                )
                self._save_all(rules)
                return rule
        return None

    def delete_rule(self, rule_id: str) -> bool:
        """Удалить правило по ID."""
        rules = self._load_all()
        filtered = [r for r in rules if r.get("id") != rule_id]
        if len(filtered) == len(rules):
            return False
        self._save_all(filtered)
        return True

    def duplicate_rule(self, rule_id: str) -> dict[str, Any] | None:
        source = self.get_rule(rule_id)
        if not source:
            return None
        return self.add_rule(
            name=f"{source.get('name', 'Шаблон')} - копия",
            prompt=source.get("prompt") or source.get("ai_improved_instruction") or "",
            raw_prompt=source.get("raw_user_prompt"),
            generated_rule=source.get("generated_rule") or {},
            fingerprint=source.get("fingerprint") or {},
            description=source.get("description"),
            domain=source.get("domain"),
            use_raw_data=source.get("use_raw_data"),
            sheet_name=source.get("sheet_name"),
            category=source.get("category"),
            table_type=source.get("table_type"),
            tags=list(source.get("tags") or []),
        )

    def find_similar_rules(self, fingerprint: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
        """Найти похожие шаблоны по fingerprint структуры таблицы."""
        scored: list[tuple[int, dict[str, Any]]] = []
        for rule in self.list_rules():
            fp = rule.get("fingerprint") or {}
            score = 0
            for key in ("table_type", "has_merged_cells", "has_month_like_columns", "has_total_rows", "header_depth"):
                if key in fingerprint and fp.get(key) == fingerprint.get(key):
                    score += 2 if key == "table_type" else 1
            if fp.get("sheet_count") == fingerprint.get("sheet_count"):
                score += 1
            if score > 0:
                item = dict(rule)
                item["similarity_score"] = score
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [rule for _, rule in scored[:limit]]
