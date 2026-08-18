"""Shared helpers for the read-only desktop bridge."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trader.reporting.read_models.runtime_state import load_runtime_state  # noqa: E402


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Enum):
        return value.name.lower()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)


def jsonable(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.name.lower()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, set):
        return [jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def dump(payload: Any) -> None:
    json.dump(
        jsonable(payload),
        sys.stdout,
        default=json_default,
        ensure_ascii=True,
        allow_nan=False,
    )
    sys.stdout.write("\n")


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def load_state() -> dict[str, Any]:
    return load_runtime_state(state_dir=REPO_ROOT / "state", config_dir=str(REPO_ROOT))
