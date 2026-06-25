from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import BinaryIO

import pandas as pd
from werkzeug.utils import secure_filename


ALLOWED_EXCEL_EXTENSIONS = {".xlsx", ".xls"}


def sanitize_upload_name(name: str) -> str:
    """Return a display-safe basename while keeping readable Unicode names."""
    basename = Path(name or "").name.strip()
    safe = secure_filename(basename)
    if safe:
        return safe
    suffix = Path(basename).suffix.lower()
    return f"excel_file{suffix if suffix in ALLOWED_EXCEL_EXTENSIONS else '.xlsx'}"


def is_excel_filename(name: str) -> bool:
    return Path(name or "").suffix.lower() in ALLOWED_EXCEL_EXTENSIONS


def validate_excel_file(path: str | Path) -> tuple[bool, str]:
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return False, "Файл пустой."
    try:
        with pd.ExcelFile(path) as book:
            sheet_names = list(book.sheet_names)
    except Exception as exc:
        return False, f"Файл не удалось прочитать как Excel: {exc}"
    if not sheet_names:
        return False, "В Excel-файле нет листов."
    return True, ""


def save_excel_upload(file: BinaryIO, input_dir: str | Path) -> tuple[str, str, Path]:
    original_name = Path(getattr(file, "filename", "") or "").name
    if not original_name or not is_excel_filename(original_name):
        raise ValueError("Поддерживаются только файлы .xlsx и .xls.")

    job_id = str(uuid.uuid4())
    stored_original = sanitize_upload_name(original_name)
    target_dir = Path(input_dir).resolve()
    meta_dir = target_dir / "meta"
    target_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{job_id}__{stored_original}"
    target_path = (target_dir / filename).resolve()
    if target_path.parent != target_dir:
        raise ValueError("Некорректное имя файла.")

    file.save(target_path)
    (meta_dir / f"{job_id}.meta.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "filename": filename,
                "original_filename": original_name,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return job_id, original_name, target_path


def clone_excel_input(
    source_path: str | Path,
    original_name: str,
    input_dir: str | Path,
) -> tuple[str, str, Path]:
    """Copy an existing source workbook into a new independent work session."""
    source = Path(source_path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError("Исходный Excel-файл больше не найден.")
    if not is_excel_filename(original_name or source.name):
        raise ValueError("Поддерживаются только файлы .xlsx и .xls.")

    job_id = str(uuid.uuid4())
    original = Path(original_name or source.name).name
    stored_original = sanitize_upload_name(original)
    target_dir = Path(input_dir).resolve()
    meta_dir = target_dir / "meta"
    target_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{job_id}__{stored_original}"
    target_path = (target_dir / filename).resolve()
    if target_path.parent != target_dir:
        raise ValueError("Некорректное имя файла.")

    import shutil

    shutil.copy2(source, target_path)
    (meta_dir / f"{job_id}.meta.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "filename": filename,
                "original_filename": original,
                "cloned_from": str(source),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return job_id, original, target_path
