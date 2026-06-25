from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re
import uuid
from datetime import datetime

from flask import current_app


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class FeedbackStore:
    """Хранилище обратной связи по инструкциям.

    В проекте не выполняется тяжёлое fine-tuning-дообучение весов модели.
    Вместо этого используется практичная схема learning from feedback:

    1. система предлагает улучшенную инструкцию;
    2. пользователь меняет только текст инструкции;
    3. система заново анализирует Excel и пересобирает внутреннее правило;
    4. правка сохраняется как пример ошибки/исправления;
    5. при следующих похожих файлах эти записи добавляются в контекст LLM
       и в эвристический fallback.

    За счёт этого AI-помощник постепенно лучше формулирует инструкции для
    однотипных файлов без ручного редактирования JSON-правил и итоговых таблиц.
    """

    def __init__(self) -> None:
        default_path = Path(current_app.root_path).parent / "instruction_feedback.json"
        self.path = Path(current_app.config.get("INSTRUCTION_FEEDBACK_FILE", default_path))

    def _load_all(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            return []

    def _save_all(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_instruction_revision(
        self,
        *,
        source_filename: str | None,
        raw_prompt: str,
        previous_ai_instruction: str,
        user_corrected_instruction: str,
        regenerated_instruction: str,
        generated_rule: dict[str, Any] | None,
        analysis_fingerprint: dict[str, Any] | None = None,
        preview_columns: list[str] | None = None,
        preview_rows_count: int | None = None,
        rule_id: str | None = None,
        action: str = "instruction_revision",
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Зафиксировать, как пользователь исправил инструкцию.

        Это главная запись для обучения на ошибках: система видит, какая
        инструкция была предложена, как пользователь её уточнил и какую новую
        инструкцию/правило система получила после повторного анализа.
        """
        previous_ai_instruction = (previous_ai_instruction or "").strip()
        user_corrected_instruction = (user_corrected_instruction or "").strip()
        regenerated_instruction = (regenerated_instruction or "").strip()
        record = {
            "id": str(uuid.uuid4()),
            "type": "instruction_revision",
            "action": action,
            "source_filename": source_filename,
            "raw_prompt": raw_prompt or "",
            "previous_ai_instruction": previous_ai_instruction,
            "user_corrected_instruction": user_corrected_instruction,
            "regenerated_instruction": regenerated_instruction,
            "generated_rule": generated_rule or {},
            "analysis_fingerprint": analysis_fingerprint or {},
            "preview_columns": preview_columns or [],
            "preview_rows_count": int(preview_rows_count or 0),
            "changed_terms": self._changed_terms(previous_ai_instruction, user_corrected_instruction),
            "rule_id": rule_id,
            "notes": notes or "",
            "created_at": _now(),
        }
        items = self._load_all()
        items.append(record)
        self._save_all(items)
        return record

    def add_template_acceptance(
        self,
        *,
        source_filename: str | None,
        raw_prompt: str,
        accepted_instruction: str,
        generated_rule: dict[str, Any] | None,
        analysis_fingerprint: dict[str, Any] | None,
        rule_id: str | None,
    ) -> dict[str, Any]:
        """Зафиксировать, что пользователь принял инструкцию как шаблон."""
        return self.add_instruction_revision(
            source_filename=source_filename,
            raw_prompt=raw_prompt,
            previous_ai_instruction=accepted_instruction,
            user_corrected_instruction=accepted_instruction,
            regenerated_instruction=accepted_instruction,
            generated_rule=generated_rule,
            analysis_fingerprint=analysis_fingerprint,
            rule_id=rule_id,
            action="template_accepted",
            notes="Пользователь сохранил инструкцию как типовой шаблон.",
        )

    # Обратная совместимость со старым патчем, где сохранялась ручная правка таблицы.
    # Новый интерфейс этот метод не использует, но наличие метода не ломает старые вызовы.
    def add_feedback(
        self,
        *,
        source_filename: str | None,
        raw_prompt: str,
        ai_instruction: str,
        user_instruction: str,
        generated_rule: dict[str, Any] | None,
        corrected_columns: list[str] | None = None,
        corrected_rows_count: int = 0,
        analysis_fingerprint: dict[str, Any] | None = None,
        training_example_id: str | None = None,
        rule_id: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        return self.add_instruction_revision(
            source_filename=source_filename,
            raw_prompt=raw_prompt,
            previous_ai_instruction=ai_instruction,
            user_corrected_instruction=user_instruction,
            regenerated_instruction=user_instruction,
            generated_rule=generated_rule,
            analysis_fingerprint=analysis_fingerprint,
            preview_columns=corrected_columns or [],
            preview_rows_count=corrected_rows_count,
            rule_id=rule_id,
            action="legacy_feedback",
            notes=notes,
        )

    def list_feedback(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = list(reversed(self._load_all()))
        return items[:limit] if limit else items

    def get_stats(self) -> dict[str, int]:
        items = self._load_all()
        return {
            "total": len(items),
            "instruction_revisions": sum(1 for item in items if item.get("type") == "instruction_revision"),
            "accepted_templates": sum(1 for item in items if item.get("action") == "template_accepted"),
            "with_rule": sum(1 for item in items if item.get("rule_id")),
        }

    def find_relevant(
        self,
        *,
        user_text: str | None,
        fingerprint: dict[str, Any] | None,
        limit: int = 5,
        require_text_overlap: bool = False,
    ) -> list[dict[str, Any]]:
        """Найти похожие прошлые исправления для текущей таблицы/инструкции."""
        text_tokens = self._tokens(user_text or "")
        fp = fingerprint or {}
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in self._load_all():
            item_fp = item.get("analysis_fingerprint") or {}
            score = 0
            if fp and item_fp:
                if item_fp.get("table_type") == fp.get("table_type"):
                    score += 5
                if item_fp.get("header_depth") == fp.get("header_depth"):
                    score += 2
                if item_fp.get("has_month_like_columns") == fp.get("has_month_like_columns"):
                    score += 2
                if item_fp.get("has_total_rows") == fp.get("has_total_rows"):
                    score += 1
                if item_fp.get("has_merged_cells") == fp.get("has_merged_cells"):
                    score += 1

            text_for_tokens = " ".join([
                str(item.get("raw_prompt") or ""),
                str(item.get("previous_ai_instruction") or item.get("ai_instruction") or ""),
                str(item.get("user_corrected_instruction") or item.get("user_instruction") or ""),
                str(item.get("regenerated_instruction") or ""),
                " ".join(str(c) for c in (item.get("preview_columns") or item.get("corrected_columns") or [])),
            ])
            item_tokens = self._tokens(text_for_tokens)
            text_overlap = len(text_tokens & item_tokens) if text_tokens and item_tokens else 0
            if require_text_overlap and text_overlap < 2:
                continue
            score += text_overlap
            if score > 0:
                copy = dict(item)
                copy["similarity_score"] = score
                copy["text_overlap"] = text_overlap
                scored.append((score, copy))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    @staticmethod
    def _tokens(text: str) -> set[str]:
        words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]{3,}", text.lower())
        stop = {"нужно", "чтобы", "таблица", "файл", "данные", "excel", "сделать", "пользователь", "строки"}
        return {word for word in words if word not in stop}

    @staticmethod
    def _changed_terms(before: str, after: str) -> list[str]:
        before_tokens = FeedbackStore._tokens(before)
        after_tokens = FeedbackStore._tokens(after)
        added = sorted(after_tokens - before_tokens)
        return added[:30]
