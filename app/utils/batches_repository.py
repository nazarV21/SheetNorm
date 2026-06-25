from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app


_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class BatchesRepository:
    """Small persistent manifest for multi-file conversions.

    Jobs remain the source of truth for execution. A batch only groups their ids
    and remembers the source folder label used by the browser UI.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or current_app.config["BATCHES_FILE"])

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save(self, batches: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(batches, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def create(
        self,
        *,
        job_ids: list[str],
        filenames: list[str],
        source_mode: str,
        folder_name: str | None = None,
    ) -> dict[str, Any]:
        with _LOCK:
            batches = self._load()
            timestamp = _now()
            batch = {
                "batch_id": str(uuid.uuid4()),
                "job_ids": list(job_ids),
                "filenames": list(filenames),
                "source_mode": source_mode,
                "folder_name": folder_name or "",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            batches.append(batch)
            self._save(batches)
            return batch

    def get(self, batch_id: str) -> dict[str, Any] | None:
        with _LOCK:
            return next((item for item in self._load() if item.get("batch_id") == batch_id), None)

    def list(self) -> list[dict[str, Any]]:
        with _LOCK:
            return sorted(self._load(), key=lambda item: item.get("created_at") or "", reverse=True)
