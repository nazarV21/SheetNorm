"""Хранилище обучающих примеров для ИИ.

Поддерживает одиночное добавление пары Excel-файлов и массовый импорт
обучающих пар из ZIP-архива или двух наборов файлов.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
import hashlib
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime

from flask import current_app


EXCEL_EXTENSIONS = {".xlsx", ".xls"}
SOURCE_DIR_MARKERS = {"source", "sources", "input", "inputs", "исходные", "исходная", "сырье", "raw"}
TARGET_DIR_MARKERS = {"target", "targets", "output", "outputs", "итоговые", "итоговая", "результат", "normalized"}
SOURCE_SUFFIXES = (
    "_source", "-source", " source",
    "_input", "-input", " input",
    "_raw", "-raw", " raw",
    "_исходная", "-исходная", " исходная",
    "_исходный", "-исходный", " исходный",
)
TARGET_SUFFIXES = (
    "_target", "-target", " target",
    "_output", "-output", " output",
    "_normalized", "-normalized", " normalized",
    "_flat", "-flat", " flat",
    "_итоговая", "-итоговая", " итоговая",
    "_результат", "-результат", " результат",
    "_плоская", "-плоская", " плоская",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TrainingExamplesStore:
    """JSON-хранилище обучающих примеров формата «исходная таблица → эталон»."""

    def __init__(self) -> None:
        self.path = Path(current_app.config.get("TRAINING_EXAMPLES_FILE", "training_examples.json"))
        self.base_dir = self.path.parent

    def _examples_dir(self) -> Path:
        examples_dir = Path(
            current_app.config.get(
                "TRAINING_EXAMPLES_DIR",
                str(self.base_dir / "training_examples"),
            )
        )
        examples_dir.mkdir(parents=True, exist_ok=True)
        return examples_dir

    def _load_all(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            return []

    def _save_all(self, examples: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8")

    def _as_stored_path(self, path: Path) -> str:
        """Сохраняем переносимый относительный путь, если файл внутри проекта."""
        try:
            return str(path.resolve().relative_to(self.base_dir.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)

    def _resolve_path(self, stored_path: str | None, example_id: str | None, kind: str) -> Path:
        """Вернуть реальный путь к файлу примера.

        Старые версии проекта сохраняли абсолютные Windows-пути. После переноса
        проекта такие пути не работают, поэтому дополнительно ищем файл по ID
        в текущей папке training_examples.
        """
        candidates: list[Path] = []
        if stored_path:
            p = Path(stored_path)
            candidates.append(p if p.is_absolute() else self.base_dir / p)
        if example_id:
            ext_candidates = [".xlsx", ".xls"]
            for ext in ext_candidates:
                candidates.append(self._examples_dir() / f"{example_id}_{kind}{ext}")
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0] if candidates else self._examples_dir() / f"missing_{kind}.xlsx"

    def _normalize_example(self, example: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(example)
        example_id = str(normalized.get("id") or "")
        source_path = self._resolve_path(normalized.get("source_path"), example_id, "source")
        target_path = self._resolve_path(normalized.get("target_path"), example_id, "target")
        normalized["source_path"] = str(source_path)
        normalized["target_path"] = str(target_path)
        normalized["source_exists"] = source_path.exists()
        normalized["target_exists"] = target_path.exists()
        return normalized

    def list_examples(self, rule_id: str | None = None) -> list[dict[str, Any]]:
        """Получить список примеров.

        Если указан rule_id, возвращаются примеры этого правила плюс глобальные
        примеры без привязки. Так модель получает общий корпус и специальные
        примеры для выбранного правила.
        """
        examples = [self._normalize_example(ex) for ex in self._load_all()]
        if rule_id:
            examples = [ex for ex in examples if ex.get("rule_id") in (None, rule_id)]
        return examples

    def get_stats(self) -> dict[str, int]:
        examples = self.list_examples()
        return {
            "total": len(examples),
            "available": sum(1 for ex in examples if ex.get("source_exists") and ex.get("target_exists")),
            "broken": sum(1 for ex in examples if not (ex.get("source_exists") and ex.get("target_exists"))),
            "with_rule": sum(1 for ex in examples if ex.get("rule_id")),
            "global": sum(1 for ex in examples if not ex.get("rule_id")),
        }

    def get_example(self, example_id: str) -> dict[str, Any] | None:
        """Получить пример по ID."""
        for example in self.list_examples():
            if example.get("id") == example_id:
                return example
        return None

    def add_example(
        self,
        rule_id: str | None,
        source_filename: str,
        target_filename: str,
        source_path: Path,
        target_path: Path,
        prompt: str | None = None,
        batch_id: str | None = None,
        skip_duplicates: bool = True,
    ) -> dict[str, Any]:
        """Добавить одну обучающую пару."""
        summary = self.add_examples_from_pairs(
            [
                {
                    "source_filename": source_filename,
                    "target_filename": target_filename,
                    "source_path": Path(source_path),
                    "target_path": Path(target_path),
                }
            ],
            rule_id=rule_id,
            prompt=prompt,
            batch_id=batch_id,
            skip_duplicates=skip_duplicates,
        )
        if summary["added_examples"]:
            return summary["added_examples"][0]
        if summary["skipped_examples"]:
            return summary["skipped_examples"][0]
        raise RuntimeError("Не удалось добавить обучающий пример")

    def add_examples_from_pairs(
        self,
        pairs: Iterable[dict[str, Any]],
        rule_id: str | None,
        prompt: str | None = None,
        batch_id: str | None = None,
        skip_duplicates: bool = True,
    ) -> dict[str, Any]:
        """Добавить несколько обучающих пар за один вызов."""
        examples = self._load_all()
        examples_dir = self._examples_dir()
        batch_id = batch_id or str(uuid.uuid4())

        existing_hashes = {
            (ex.get("source_sha256"), ex.get("target_sha256"), ex.get("rule_id"))
            for ex in examples
            if ex.get("source_sha256") and ex.get("target_sha256")
        }

        added: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[str] = []

        for pair in pairs:
            source_path = Path(pair["source_path"])
            target_path = Path(pair["target_path"])
            source_filename = str(pair.get("source_filename") or source_path.name)
            target_filename = str(pair.get("target_filename") or target_path.name)

            if not source_path.exists() or not target_path.exists():
                errors.append(f"Файлы пары не найдены: {source_filename} / {target_filename}")
                continue

            source_hash = self._sha256(source_path)
            target_hash = self._sha256(target_path)
            duplicate_key = (source_hash, target_hash, rule_id)
            if skip_duplicates and duplicate_key in existing_hashes:
                skipped.append(
                    {
                        "source_filename": source_filename,
                        "target_filename": target_filename,
                        "reason": "duplicate",
                    }
                )
                continue

            example_id = str(uuid.uuid4())
            source_ext = source_path.suffix.lower() if source_path.suffix.lower() in EXCEL_EXTENSIONS else ".xlsx"
            target_ext = target_path.suffix.lower() if target_path.suffix.lower() in EXCEL_EXTENSIONS else ".xlsx"
            source_example_path = examples_dir / f"{example_id}_source{source_ext}"
            target_example_path = examples_dir / f"{example_id}_target{target_ext}"
            shutil.copy2(source_path, source_example_path)
            shutil.copy2(target_path, target_example_path)

            example = {
                "id": example_id,
                "rule_id": rule_id,
                "source_filename": source_filename,
                "target_filename": target_filename,
                "source_path": self._as_stored_path(source_example_path),
                "target_path": self._as_stored_path(target_example_path),
                "source_sha256": source_hash,
                "target_sha256": target_hash,
                "prompt": prompt,
                "batch_id": batch_id,
                "created_at": _now(),
            }
            examples.append(example)
            existing_hashes.add(duplicate_key)
            added.append(example)

        self._save_all(examples)
        return {
            "batch_id": batch_id,
            "added": len(added),
            "skipped": len(skipped),
            "errors": errors,
            "added_examples": added,
            "skipped_examples": skipped,
        }

    def import_from_zip(
        self,
        zip_path: Path,
        rule_id: str | None,
        prompt: str | None = None,
        skip_duplicates: bool = True,
    ) -> dict[str, Any]:
        """Импортировать обучающие пары из ZIP-архива.

        Поддерживаются форматы:
        - `*_source.xlsx` + `*_target.xlsx`;
        - папки `source/` и `target/` с одинаковыми именами файлов;
        - архив, созданный страницей экспорта/датасетом проекта.
        """
        if not zipfile.is_zipfile(zip_path):
            return {"added": 0, "skipped": 0, "errors": ["Файл не является ZIP-архивом"], "added_examples": [], "skipped_examples": []}

        batch_id = str(uuid.uuid4())
        try:
            with tempfile.TemporaryDirectory(prefix="training_zip_") as tmp:
                tmp_dir = Path(tmp)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    self._safe_extract(zf, tmp_dir)
                pairs, errors = self._find_pairs(tmp_dir)
                summary = self.add_examples_from_pairs(
                    pairs,
                    rule_id=rule_id,
                    prompt=prompt,
                    batch_id=batch_id,
                    skip_duplicates=skip_duplicates,
                )
                summary["errors"] = errors + summary.get("errors", [])
                return summary
        except Exception as exc:
            return {
                "batch_id": batch_id,
                "added": 0,
                "skipped": 0,
                "errors": [f"Ошибка чтения ZIP-архива: {exc}"],
                "added_examples": [],
                "skipped_examples": [],
            }

    def import_from_directories(
        self,
        source_dir: Path,
        target_dir: Path,
        rule_id: str | None,
        prompt: str | None = None,
        skip_duplicates: bool = True,
    ) -> dict[str, Any]:
        """Импортировать пары из двух папок: source_dir и target_dir."""
        pairs: list[dict[str, Any]] = []
        errors: list[str] = []
        source_files = [p for p in source_dir.rglob("*") if self._is_excel(p)]
        target_files = [p for p in target_dir.rglob("*") if self._is_excel(p)]

        sources = {self._pair_key(p, "source"): p for p in source_files}
        targets = {self._pair_key(p, "target"): p for p in target_files}

        for key in sorted(set(sources) & set(targets)):
            pairs.append(
                {
                    "source_path": sources[key],
                    "target_path": targets[key],
                    "source_filename": sources[key].name,
                    "target_filename": targets[key].name,
                }
            )
        for key in sorted(set(sources) - set(targets)):
            errors.append(f"Не найден итоговый файл для исходного: {sources[key].name}")
        for key in sorted(set(targets) - set(sources)):
            errors.append(f"Не найден исходный файл для итогового: {targets[key].name}")

        summary = self.add_examples_from_pairs(pairs, rule_id=rule_id, prompt=prompt, skip_duplicates=skip_duplicates)
        summary["errors"] = errors + summary.get("errors", [])
        return summary

    def delete_example(self, example_id: str) -> bool:
        """Удалить пример по ID."""
        examples = self._load_all()
        example = None
        filtered = []

        for ex in examples:
            if ex.get("id") == example_id:
                example = ex
            else:
                filtered.append(ex)

        if not example:
            return False

        normalized = self._normalize_example(example)
        for key in ("source_path", "target_path"):
            try:
                Path(normalized[key]).unlink(missing_ok=True)
            except Exception:
                pass

        self._save_all(filtered)
        return True

    def _find_pairs(self, root: Path) -> tuple[list[dict[str, Any]], list[str]]:
        excel_files = [p for p in root.rglob("*") if self._is_excel(p)]
        sources: dict[str, Path] = {}
        targets: dict[str, Path] = {}
        errors: list[str] = []

        for path in excel_files:
            role = self._detect_role(path, root)
            if role == "source":
                sources.setdefault(self._pair_key(path, "source"), path)
            elif role == "target":
                targets.setdefault(self._pair_key(path, "target"), path)

        pairs: list[dict[str, Any]] = []
        for key in sorted(set(sources) & set(targets)):
            pairs.append(
                {
                    "source_path": sources[key],
                    "target_path": targets[key],
                    "source_filename": sources[key].name,
                    "target_filename": targets[key].name,
                }
            )

        if not pairs:
            errors.append(
                "В архиве не найдено пар. Используйте имена *_source.xlsx и *_target.xlsx "
                "или папки source/target с одинаковыми именами файлов."
            )
        else:
            for key in sorted(set(sources) - set(targets))[:20]:
                errors.append(f"Не найден target-файл для: {sources[key].name}")
            for key in sorted(set(targets) - set(sources))[:20]:
                errors.append(f"Не найден source-файл для: {targets[key].name}")

        return pairs, errors

    def _safe_extract(self, zf: zipfile.ZipFile, target_dir: Path) -> None:
        target_root = target_dir.resolve()
        for member in zf.infolist():
            member_path = target_dir / member.filename
            resolved = member_path.resolve()
            if not str(resolved).startswith(str(target_root)):
                raise ValueError(f"Небезопасный путь в ZIP: {member.filename}")
            if member.is_dir():
                resolved.mkdir(parents=True, exist_ok=True)
                continue
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, resolved.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    def _detect_role(self, path: Path, root: Path) -> str | None:
        rel_parts = [part.lower() for part in path.relative_to(root).parts[:-1]]
        stem = path.stem.lower()
        if any(part in SOURCE_DIR_MARKERS for part in rel_parts) or self._has_suffix(stem, SOURCE_SUFFIXES):
            return "source"
        if any(part in TARGET_DIR_MARKERS for part in rel_parts) or self._has_suffix(stem, TARGET_SUFFIXES):
            return "target"
        return None

    @staticmethod
    def _has_suffix(stem: str, suffixes: tuple[str, ...]) -> bool:
        return any(stem.endswith(suffix) for suffix in suffixes) or any(stem.startswith(suffix.strip("_-") + "_") for suffix in suffixes)

    def _pair_key(self, path: Path, role: str) -> str:
        stem = path.stem.lower().strip()
        suffixes = SOURCE_SUFFIXES if role == "source" else TARGET_SUFFIXES
        for suffix in suffixes:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        # source_01_name / target_01_name
        stem = re.sub(r"^(source|target|input|output|raw|исходная|итоговая|результат)[_\-\s]+", "", stem)
        stem = re.sub(r"[_\-\s]+", "_", stem)
        return stem.strip("_")

    @staticmethod
    def _is_excel(path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in EXCEL_EXTENSIONS and not path.name.startswith("~$")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
