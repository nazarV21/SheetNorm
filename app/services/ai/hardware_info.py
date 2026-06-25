from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import platform
from pathlib import Path
import subprocess
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - optional fallback
    psutil = None  # type: ignore


@dataclass(frozen=True)
class HardwareInfo:
    operating_system: str
    architecture: str
    logical_cpu_count: int
    physical_cpu_count: int
    total_ram_gb: float | None
    available_ram_gb: float | None
    container_memory_limit_gb: float | None
    gpu_backend: str
    gpu_name: str | None
    gpu_vram_gb: float | None

    @property
    def effective_available_ram_gb(self) -> float | None:
        values = [value for value in (self.available_ram_gb, self.container_memory_limit_gb) if value and value > 0]
        return min(values) if values else None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["effective_available_ram_gb"] = self.effective_available_ram_gb
        return data


class HardwareInfoService:
    def collect(self) -> HardwareInfo:
        logical = os.cpu_count() or 1
        physical = psutil.cpu_count(logical=False) if psutil else None
        total_ram = available_ram = None
        if psutil:
            memory = psutil.virtual_memory()
            total_ram = round(memory.total / (1024**3), 2)
            available_ram = round(memory.available / (1024**3), 2)
        gpu_name, gpu_vram = self._nvidia_info()
        return HardwareInfo(
            operating_system=f"{platform.system()} {platform.release()}".strip(),
            architecture=platform.machine() or "unknown",
            logical_cpu_count=int(logical),
            physical_cpu_count=int(physical or logical),
            total_ram_gb=total_ram,
            available_ram_gb=available_ram,
            container_memory_limit_gb=self._container_memory_limit_gb(),
            gpu_backend="CUDA" if gpu_name else "CPU",
            gpu_name=gpu_name,
            gpu_vram_gb=gpu_vram,
        )

    @staticmethod
    def _container_memory_limit_gb() -> float | None:
        candidates = (
            Path("/sys/fs/cgroup/memory.max"),
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        )
        for path in candidates:
            try:
                value = path.read_text(encoding="utf-8").strip()
                if value == "max":
                    return None
                amount = int(value)
                if amount <= 0 or amount >= 2**60:
                    return None
                return round(amount / (1024**3), 2)
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _nvidia_info() -> tuple[str | None, float | None]:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None, None
        if result.returncode != 0 or not result.stdout.strip():
            return None, None
        first_line = result.stdout.strip().splitlines()[0]
        parts = [part.strip() for part in first_line.split(",", 1)]
        name = parts[0] or None
        try:
            vram = round(float(parts[1]) / 1024, 2) if len(parts) > 1 else None
        except ValueError:
            vram = None
        return name, vram
