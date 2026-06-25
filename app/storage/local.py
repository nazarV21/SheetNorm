from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import shutil
import uuid


@dataclass(frozen=True)
class StoredFile:
    storage_key: str
    original_name: str
    size_bytes: int
    sha256: str


class LocalStorageBackend:
    """Small local storage backend with traversal-safe storage keys."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_file(self, source: str | Path, *, prefix: str = "", original_name: str | None = None) -> StoredFile:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        original = Path(original_name or source_path.name).name
        suffix = Path(original).suffix.lower()
        safe_prefix = self._normalise_key(prefix) if prefix else ""
        filename = f"{uuid.uuid4()}{suffix}"
        storage_key = "/".join(part for part in (safe_prefix, filename) if part)
        target = self.open_path(storage_key, require_exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)

        digest = sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return StoredFile(
            storage_key=storage_key,
            original_name=original,
            size_bytes=target.stat().st_size,
            sha256=digest.hexdigest(),
        )

    def open_path(self, storage_key: str, *, require_exists: bool = True) -> Path:
        normalised = self._normalise_key(storage_key)
        target = (self.root / normalised).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Storage key escapes the configured root")
        if require_exists and not target.is_file():
            raise FileNotFoundError(storage_key)
        return target

    def delete(self, storage_key: str) -> bool:
        path = self.open_path(storage_key, require_exists=False)
        if not path.exists():
            return False
        path.unlink()
        return True

    @staticmethod
    def _normalise_key(value: str) -> str:
        raw = str(value or "").replace("\\", "/").strip("/")
        parts = [part for part in raw.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts):
            raise ValueError("Invalid storage key")
        return "/".join(parts)
