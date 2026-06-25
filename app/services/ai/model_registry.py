from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any


_PARAMETER_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z])")
_QUANT_RE = re.compile(r"(?<![A-Za-z0-9])(Q\d(?:_[A-Z0-9]+)*)(?![A-Za-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class LocalModelInfo:
    id: str
    display_name: str
    filename: str
    relative_path: str
    size_bytes: int
    size_gb: float
    modified_at: str
    family_hint: str | None = None
    parameter_hint: float | None = None
    quantization_hint: str | None = None
    status: str = "available"
    status_message: str = ""
    metadata_source: str = "filename"
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModelRegistry:
    """Discovers readable GGUF files inside one configured directory."""

    def __init__(self, models_dir: str | Path):
        self.root = Path(models_dir).expanduser().resolve()

    def scan_models(self) -> list[LocalModelInfo]:
        self.root.mkdir(parents=True, exist_ok=True)
        models: list[LocalModelInfo] = []
        for candidate in sorted(self.root.rglob("*.gguf"), key=lambda item: str(item).lower()):
            if candidate.name.startswith(".") or candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            if not resolved.is_relative_to(self.root) or not resolved.is_file():
                continue
            models.append(self._build_info(resolved))
        return models

    def refresh(self) -> list[LocalModelInfo]:
        return self.scan_models()

    def get_model(self, model_id: str) -> LocalModelInfo:
        for model in self.scan_models():
            if model.id == model_id:
                return model
        raise KeyError(f"Unknown model id: {model_id}")

    def get_by_relative_path(self, relative_path: str | None) -> LocalModelInfo | None:
        if not relative_path:
            return None
        normalised = str(relative_path).replace("\\", "/").strip("/")
        if not normalised or ".." in Path(normalised).parts:
            return None
        for model in self.scan_models():
            if model.relative_path == normalised:
                return model
        return None

    def resolve_path(self, relative_path: str) -> Path:
        model = self.get_by_relative_path(relative_path)
        if model is None:
            raise FileNotFoundError(relative_path)
        resolved = (self.root / model.relative_path).resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Model path escapes AI_MODELS_DIR")
        return resolved

    def _build_info(self, path: Path) -> LocalModelInfo:
        relative = path.relative_to(self.root).as_posix()
        stat = path.stat()
        filename_meta = self._metadata_from_filename(path.stem)
        sidecar_meta, sidecar_status = self._load_sidecar(path)
        metadata = {**filename_meta, **sidecar_meta}
        status = "available"
        status_message = sidecar_status
        try:
            with path.open("rb") as stream:
                if stream.read(4) != b"GGUF":
                    status = "warning"
                    status_message = "Файл имеет расширение GGUF, но сигнатура GGUF не обнаружена."
        except OSError as exc:
            status = "unreadable"
            status_message = f"Файл недоступен для чтения: {exc}"

        model_id = sha256(relative.encode("utf-8")).hexdigest()[:20]
        display_name = str(metadata.get("display_name") or path.stem.replace("_", " ").replace("-", " "))
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        return LocalModelInfo(
            id=model_id,
            display_name=display_name,
            filename=path.name,
            relative_path=relative,
            size_bytes=stat.st_size,
            size_gb=round(stat.st_size / (1024**3), 2),
            modified_at=modified,
            family_hint=metadata.get("family"),
            parameter_hint=metadata.get("parameters_b"),
            quantization_hint=metadata.get("quantization"),
            status=status,
            status_message=status_message,
            metadata_source="sidecar" if sidecar_meta else "filename",
            metadata=metadata,
        )

    @staticmethod
    def _metadata_from_filename(stem: str) -> dict[str, Any]:
        parameter_match = _PARAMETER_RE.search(stem)
        quant_match = _QUANT_RE.search(stem)
        parameter = float(parameter_match.group(1)) if parameter_match else None
        quant = quant_match.group(1).upper() if quant_match else None
        family_part = stem
        cutoff_positions = [match.start() for match in (parameter_match, quant_match) if match]
        if cutoff_positions:
            family_part = stem[: min(cutoff_positions)]
        family = re.sub(r"[-_]+", " ", family_part).strip() or None
        return {
            "family": family,
            "parameters_b": parameter,
            "quantization": quant,
        }

    @staticmethod
    def _load_sidecar(model_path: Path) -> tuple[dict[str, Any], str]:
        sidecar = model_path.with_suffix(".json")
        if not sidecar.exists():
            return {}, ""
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("sidecar must contain a JSON object")
            allowed = {
                "display_name",
                "family",
                "parameters_b",
                "quantization",
                "recommended_ram_gb",
                "minimum_ram_gb",
                "recommended_context",
                "description",
            }
            return {key: payload[key] for key in allowed if key in payload}, ""
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {}, f"Не удалось прочитать metadata sidecar: {exc}"
