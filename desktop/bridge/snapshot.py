"""Read-only snapshot for the desktop cockpit.

Reuses the TUI read model, then slims blobs so the UI can poll every few seconds.
Never writes. Never talks to the broker.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trader.reporting.read_models.runtime_state import load_runtime_state  # noqa: E402

DECISION_KEYS = (
    "action",
    "confidence",
    "cycle_ts",
    "decision_id",
    "decision_reason_code",
    "decision_source",
    "executed",
    "intent",
    "llm_model",
    "llm_provider",
    "model_called",
    "next_wake_in_minutes",
    "opportunity_side",
    "price",
    "qty",
    "rationale",
    "reason",
    "sequence",
    "symbol",
    "ts",
)
RUNTIME_KEYS = (
    "armed_plan_id",
    "blocked",
    "data_source",
    "indicator_watch_created",
    "reject_reason",
    "trade_plan_created",
)
WATCH_KEYS = (
    "conditions",
    "expires_at",
    "logic",
    "purpose",
    "symbol",
    "watch_id",
)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _jsonable(value: Any) -> Any:
    """JS JSON.parse rejects NaN/Infinity that Python json.dump emits by default."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _slim_decision(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    slim = {key: row.get(key) for key in DECISION_KEYS if row.get(key) is not None}
    runtime = _as_dict(row.get("runtime"))
    kept_runtime = {key: runtime[key] for key in RUNTIME_KEYS if key in runtime}
    if kept_runtime:
        slim["runtime"] = kept_runtime
    watch = _as_dict(_as_dict(row.get("decision")).get("indicator_watch"))
    if watch:
        slim["indicator_watch"] = {
            key: watch.get(key) for key in WATCH_KEYS if watch.get(key) is not None
        }
    return slim


def _slim_watch(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {key: row.get(key) for key in WATCH_KEYS if row.get(key) is not None}


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid_file(state_dir: Path) -> int | None:
    raw = (state_dir / "daemon.pid").read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return int(raw)


def _equity_series(state_dir: Path, *, max_points: int = 480) -> list[dict[str, Any]]:
    path = state_dir / "history.jsonl"
    if not path.exists():
        return []
    points: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("equity") is None:
            continue
        try:
            equity = float(row["equity"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(equity) or equity <= 0:
            continue
        points.append(
            {
                "ts": str(row.get("ts") or ""),
                "equity": equity,
                "cash": row.get("cash"),
            }
        )
    if len(points) <= max_points:
        return points
    stride = max(1, len(points) // max_points)
    sampled = points[::stride]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled[-max_points:]


def build_snapshot() -> dict[str, Any]:
    state_dir = REPO_ROOT / "state"
    raw = load_runtime_state(state_dir=state_dir, config_dir=str(REPO_ROOT))
    status = _as_dict(raw.get("daemon_status"))
    try:
        pid = status.get("pid") if isinstance(status.get("pid"), int) else _read_pid_file(state_dir)
    except (OSError, ValueError):
        pid = status.get("pid") if isinstance(status.get("pid"), int) else None

    decisions = [slim for row in _as_list(raw.get("decisions")) if (slim := _slim_decision(row))]
    recent = [slim for row in _as_list(raw.get("recent_decisions")) if (slim := _slim_decision(row))]
    watches = [slim for row in _as_list(raw.get("indicator_watches")) if (slim := _slim_watch(row))]
    plans = []
    for row in _as_list(raw.get("trade_plans")):
        if isinstance(row, dict):
            plans.append(row)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(REPO_ROOT),
        "source": raw.get("source"),
        "dry_run": raw.get("dry_run"),
        "starting_cash": raw.get("starting_cash"),
        "ts": raw.get("ts"),
        "daemon": {
            "pid": pid,
            "alive": _pid_alive(pid if isinstance(pid, int) else None),
            "phase": status.get("phase"),
            "dry_run": status.get("dry_run", raw.get("dry_run")),
            "current_symbol": status.get("current_symbol"),
            "decisions_done": status.get("decisions_done"),
            "symbols_due": status.get("symbols_due"),
            "symbols_total": status.get("symbols_total"),
            "model_calls_used": status.get("model_calls_used"),
            "max_model_calls_per_cycle": status.get("max_model_calls_per_cycle"),
            "ts": status.get("ts"),
        },
        "portfolio": raw.get("portfolio") if isinstance(raw.get("portfolio"), dict) else {},
        "kpis": raw.get("kpis") if isinstance(raw.get("kpis"), dict) else {},
        "equity_series": _equity_series(state_dir),
        "decisions": decisions,
        "recent_decisions": recent,
        "trade_plans": plans,
        "indicator_watches": watches,
        "armed_plans": [
            slim for row in _as_list(raw.get("armed_plans")) if (slim := _slim_watch(row))
        ],
        "default_next_wake": raw.get("default_next_wake"),
        "symbol_wakes": raw.get("symbol_wakes") if isinstance(raw.get("symbol_wakes"), dict) else {},
        "stale_market_data": (
            raw.get("stale_market_data") if isinstance(raw.get("stale_market_data"), dict) else {}
        ),
        "queue_worker_activity": (
            raw.get("queue_worker_activity")
            if isinstance(raw.get("queue_worker_activity"), dict)
            else {}
        ),
        "open_venues_list": _as_list(raw.get("open_venues_list")),
        "company_map": raw.get("company_map") if isinstance(raw.get("company_map"), dict) else {},
        "universe_symbols": _as_list(raw.get("universe_symbols")),
        "attribution": {
            "recent_trips": _as_list(_as_dict(raw.get("attribution")).get("recent_trips"))[:12]
        },
        "kill_active": (REPO_ROOT / "KILL").exists(),
    }


def main() -> None:
    json.dump(
        _jsonable(build_snapshot()),
        sys.stdout,
        default=_json_default,
        ensure_ascii=True,
        allow_nan=False,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
