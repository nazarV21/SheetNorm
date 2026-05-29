from __future__ import annotations

from pathlib import Path
import json


class FileManager:
    def __init__(self, input_dir: str | Path, output_dir: str | Path) -> None:
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.meta_dir = self.input_dir / "meta"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)

    def resolve_input(self, job_id: str) -> Path:
        """Найти входной файл по job_id.

        Основной способ — через мета-файл <job_id>.meta.json,
        в котором сохранено исходное имя файла. Для совместимости
        остаётся поиск по старой схеме job_id__*
        """
        meta_path = self.meta_dir / f"{job_id}.meta.json"
        if meta_path.exists():
            data = json.load(meta_path.open("r", encoding="utf-8"))
            filename = data.get("filename")
            if filename:
                path = self.input_dir / filename
                if path.exists():
                    return path

        # fallback для старых файлов
        pattern = f"{job_id}__"
        for file in self.input_dir.glob(f"{pattern}*"):
            return file

        return self.input_dir / f"{job_id}.xlsx"

    def get_original_filename(self, job_id: str) -> str:
        """Вернуть исходное имя файла."""
        meta_path = self.meta_dir / f"{job_id}.meta.json"
        if meta_path.exists():
            data = json.load(meta_path.open("r", encoding="utf-8"))
            if "original_filename" in data:
                return data["original_filename"]
            if "filename" in data:
                return data["filename"]

        # fallback: берём имя реального файла
        src = self.resolve_input(job_id)
        return src.name

    def prepare_output(self, job_id: str, extension: str = "csv") -> Path:
        ext = extension if extension.startswith(".") else f".{extension}"
        # Имя итогового файла: исходное_имя + приписка _converted
        try:
            original = self.get_original_filename(job_id)
            base = Path(original).stem
            filename = f"{base}_converted{ext}"
        except FileNotFoundError:
            filename = f"{job_id}_converted{ext}"
        path = self.output_dir / filename
        if path.exists():
            path.unlink()
        return path

