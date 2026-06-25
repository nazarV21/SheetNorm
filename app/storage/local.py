from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from werkzeug.utils import secure_filename


@dataclass(frozen=True)
class StoredObject:
    storage_key: str
    size_bytes: int
    sha256: str


class LocalStorageBackend:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_file(self, source: str | Path, *, prefix: str = "artifacts", original_name: str | None = None) -> StoredObject:
        source_path = Path(source)
        name = secure_filename(original_name or source_path.name) or "file"
        key = f"{prefix.strip('/')}/{uuid.uuid4()}__{name}"
        target = self._resolve_key_for_write(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)
        return StoredObject(storage_key=key, size_bytes=target.stat().st_size, sha256=self._sha256(target))

    def open_path(self, storage_key: str) -> Path:
        path = (self.root / storage_key).resolve()
        if not self._is_inside_root(path):
            raise ValueError("Storage key escapes storage root.")
        if not path.exists():
            raise FileNotFoundError(storage_key)
        return path

    def delete(self, storage_key: str) -> bool:
        path = self.open_path(storage_key)
        path.unlink()
        return True

    def _resolve_key_for_write(self, storage_key: str) -> Path:
        path = (self.root / storage_key).resolve()
        if not self._is_inside_root(path):
            raise ValueError("Storage key escapes storage root.")
        return path

    def _is_inside_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

