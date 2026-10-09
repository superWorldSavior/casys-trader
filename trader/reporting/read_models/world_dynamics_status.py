"""Cheap operational projection of the automatic shadow dynamics worker.

The saved report describes the last publication. Process liveness is checked
separately; reading this view never loads market evidence or trains a model.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.world_episode import parse_utc_timestamp


WORLD_DYNAMICS_STATUS_SCHEMA = "world_dynamics_status.v1"
_STATUS_FILE = "world_dynamics_status.json"
_MAX_STATUS_BYTES = 1_048_576
_REPORT_COMMAND = "casys-trader world dynamics status --json"


def _process_alive(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


def _owner_pid(state_dir: Path) -> int | None:
    try:
        return int((state_dir / "daemon.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _validate(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("status must be a JSON object")
    if payload.get("schema_version") != WORLD_DYNAMICS_STATUS_SCHEMA:
        raise ValueError("unsupported dynamics status schema")
    for field, expected in (("authority", "shadow_only"), ("decision_effect", "none"), ("recommendation", "NO_GO")):
        if payload.get(field) != expected:
            raise ValueError(f"invalid shadow status {field}")
    if payload.get("causal_claim", False) is not False or payload.get("pnl_claim", False) is not False:
        raise ValueError("operational status cannot claim causal or PnL evidence")
    if not isinstance(payload.get("status"), str) or not payload["status"].strip():
        raise ValueError("status requires a non-empty status")
    if not isinstance(payload.get("enabled"), bool):
        raise ValueError("status requires a boolean enabled flag")
    pid = payload.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2_147_483_647:
        raise ValueError("status requires a positive process ID")
    started = parse_utc_timestamp(payload.get("started_at"), "started_at")
    updated = parse_utc_timestamp(payload.get("updated_at"), "updated_at")
    if updated < started:
        raise ValueError("status updated_at precedes worker started_at")
    for field in ("captured_at", "last_run_at"):
        if payload.get(field) is not None:
            parse_utc_timestamp(payload[field], field)
    series = payload.get("series", [])
    if not isinstance(series, list) or any(not isinstance(row, dict) for row in series):
        raise ValueError("status series must be a list of objects")
    return payload


def read_world_dynamics_status(
    state_dir: str | Path, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Read the last report and distinguish its owner from a stopped daemon."""

    root = Path(state_dir)
    path = root / _STATUS_FILE
    current = parse_utc_timestamp(now or datetime.now(timezone.utc), "now")
    base: dict[str, Any] = {
        "schema_version": WORLD_DYNAMICS_STATUS_SCHEMA,
        "authority": "shadow_only", "decision_effect": "none", "recommendation": "NO_GO",
        "causal_claim": False, "pnl_claim": False,
        "status_path": str(path), "report_command": _REPORT_COMMAND,
        "observed_at": current.isoformat(),
    }
    try:
        if path.stat().st_size > _MAX_STATUS_BYTES:
            raise ValueError("status file exceeds the operational read limit")
        payload = _validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {**base, "status": "not_started", "reason": "status_not_written", "enabled": None,
                "process_alive": None, "series": [], "last_report": None}
    except (OSError, ValueError, TypeError) as exc:
        return {**base, "status": "unavailable", "reason": "invalid_status_file", "enabled": None,
                "process_alive": None, "series": [], "last_report": None,
                "error": f"{type(exc).__name__}:{exc}"}
    last_report = payload.get("last_report")
    if not isinstance(last_report, dict):
        last_report = {key: payload[key] for key in (
            "status", "reason", "captured_at", "last_run_at", "series", "report_paths",
        ) if key in payload}
    alive = _process_alive(payload["pid"])
    owner = _owner_pid(root)
    projected = {**payload, **base, "process_alive": alive, "last_report": last_report,
                 "owner_matches_pid_file": owner == payload["pid"],
                 "worker_state_as_of": payload["updated_at"]}
    projected.pop("running", None)
    if alive is False or owner != payload["pid"]:
        projected.update(status="worker_stopped", reason=(
            "process_not_alive" if alive is False else "daemon_owner_changed" if owner else "daemon_not_running"
        ), running=False)
    elif alive is None:
        projected.update(status="unavailable", reason="process_liveness_unverified", running=False)
    # No inference that a model is busy is made from a live process: the saved
    # status and timestamps remain the worker's last publication, not a probe.
    return projected


__all__ = ["WORLD_DYNAMICS_STATUS_SCHEMA", "read_world_dynamics_status"]
