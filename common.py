from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
SOURCE_STATE_FILE = RUNTIME_DIR / "source_positions.json"
COPY_MAP_FILE = RUNTIME_DIR / "copy_map.json"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            value = json.load(fh)
            return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def parse_symbol_map(raw: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            source, target = part.split(":", 1)
            result[source.strip()] = target.strip()
    return result


def normalize_volume(volume: float, volume_min: float, volume_max: float, volume_step: float) -> float:
    if volume_step <= 0:
        volume_step = 0.01
    clipped = min(max(volume, volume_min), volume_max)
    steps = round((clipped - volume_min) / volume_step)
    normalized = volume_min + (steps * volume_step)
    precision = max(0, len(f"{volume_step:.10f}".rstrip("0").split(".")[-1]))
    return round(min(max(normalized, volume_min), volume_max), precision)
