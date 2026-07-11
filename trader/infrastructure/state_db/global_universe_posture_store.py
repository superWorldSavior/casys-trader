"""Append-only store for the advisory global universe posture."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Mapping

from trader.domain.universe.global_posture import GlobalUniversePosture
from trader.infrastructure.state_db.shadow import write_json_atomic


class GlobalUniversePostureStore:
    """Persist changed postures and expose one atomic current projection."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._write_lock = threading.RLock()

    @property
    def current_path(self) -> Path:
        return self.base_dir / "current.json"

    def read_current(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("posture_id") else None

    def append_if_changed(
        self,
        posture: GlobalUniversePosture,
    ) -> tuple[dict[str, Any], dict[str, str], bool]:
        payload = _validated_posture(posture)
        with self._write_lock:
            current = self.read_current()
            if current is not None and current.get("posture_id") == payload["posture_id"]:
                return current, _ref(current), False
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with (self.base_dir / f"{payload['as_of'][:10]}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            write_json_atomic(self.current_path, payload)
        return payload, _ref(payload), True


def _validated_posture(posture: GlobalUniversePosture) -> dict[str, Any]:
    if not isinstance(posture, GlobalUniversePosture):
        raise TypeError("global universe posture must be a GlobalUniversePosture")
    payload = posture.to_dict()
    as_of = str(payload.get("as_of") or "").strip()
    if len(as_of) < 10:
        raise ValueError("global universe posture as_of must start with YYYY-MM-DD")
    payload["as_of"] = as_of
    semantic = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["posture_id"] = f"global_universe_posture:v1:{hashlib.sha256(semantic.encode()).hexdigest()[:16]}"
    return payload


def _ref(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "posture_id": str(payload.get("posture_id") or ""),
        "as_of": str(payload.get("as_of") or ""),
        "gross_mode": str(payload.get("gross_mode") or ""),
        "net_bias": str(payload.get("net_bias") or ""),
    }


__all__ = ["GlobalUniversePostureStore"]
