"""Compact meta-performance payload for the runtime agent and consolidator."""

from __future__ import annotations

import json
from pathlib import Path

from . import decision_audit

DEFAULT_HORIZONS = ("1h", "4h", "1d")


def _read_audit(state_dir: Path) -> tuple[dict | None, str | None]:
    path = state_dir / "decision_audit.json"
    if not path.exists():
        return None, "decision_audit_missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, "decision_audit_invalid_json"
    except OSError:
        return None, "decision_audit_unreadable"
    if not isinstance(payload, dict):
        return None, "decision_audit_invalid_schema"
    return payload, None


def _pct_metric(metrics: dict, key: str) -> float | None:
    value = metrics.get(key)
    return value if isinstance(value, (int, float)) else None


def _hold_reason_row(reason_code: str, metrics: dict) -> dict:
    return {
        "reason_code": reason_code,
        "known": int(metrics.get("known") or 0),
        "good": int(metrics.get("good") or 0),
        "missed": int(metrics.get("missed") or 0),
        "good_known_pct": _pct_metric(metrics, "good_known_pct"),
        "missed_known_pct": _pct_metric(metrics, "missed_known_pct"),
    }


def _trade_reason_row(action: str, reason_code: str, metrics: dict) -> dict:
    return {
        "action": action,
        "reason_code": reason_code,
        "known": int(metrics.get("known") or 0),
        "good": int(metrics.get("good") or 0),
        "bad": int(metrics.get("bad") or 0),
        "neutral": int(metrics.get("neutral") or 0),
        "good_known_pct": _pct_metric(metrics, "good_known_pct"),
        "bad_known_pct": _pct_metric(metrics, "bad_known_pct"),
        "neutral_known_pct": _pct_metric(metrics, "neutral_known_pct"),
    }


def _last_known_ts(rows: list[dict], horizon: str) -> str | None:
    known = []
    for row in rows:
        verdict = ((row.get("audits") or {}).get(horizon) or {}).get("verdict")
        if verdict in {"good", "bad", "neutral", "missed"} and row.get("cycle_ts"):
            known.append(str(row["cycle_ts"]))
    return max(known) if known else None


def _horizon_payload(audit: dict, horizon: str, *, top_n: int) -> dict:
    metrics = ((audit.get("metrics") or {}).get(horizon) or {})
    by_reason = ((audit.get("metrics_by_reason") or {}).get(horizon) or {})
    hold_metrics = by_reason.get("HOLD") if isinstance(by_reason, dict) else {}
    hold_rows = [
        _hold_reason_row(str(reason_code), reason_metrics)
        for reason_code, reason_metrics in (hold_metrics or {}).items()
        if int(reason_metrics.get("known") or 0) > 0
    ]
    hold_rows.sort(key=lambda row: (row.get("missed_known_pct") or 0.0, row["known"]), reverse=True)

    trade_rows: list[dict] = []
    for action in ("BUY", "SELL"):
        action_metrics = by_reason.get(action) if isinstance(by_reason, dict) else {}
        for reason_code, reason_metrics in (action_metrics or {}).items():
            if int(reason_metrics.get("known") or 0) <= 0:
                continue
            trade_rows.append(_trade_reason_row(action, str(reason_code), reason_metrics))
    trade_rows.sort(key=lambda row: (row.get("bad_known_pct") or 0.0, row["known"]), reverse=True)

    return {
        "global": {
            key: metrics.get(key)
            for key in (
                "total",
                "known",
                "unknown",
                "machine",
                "good_known_pct",
                "bad_known_pct",
                "missed_known_pct",
                "coverage_pct",
            )
        },
        "last_known_decision_ts": _last_known_ts(audit.get("rows") or [], horizon),
        "hold_quality_by_reason": hold_rows[:top_n],
        "trade_quality_by_reason": trade_rows[:top_n],
    }


def compute_meta_performance(
    state_dir: Path,
    *,
    horizons: tuple[str, ...] = DEFAULT_HORIZONS,
    top_n: int = 8,
) -> dict:
    audit, error_code = _read_audit(state_dir)
    if audit is None:
        return {"available": False, "reason": error_code}
    if "metrics_by_reason" not in audit and isinstance(audit.get("rows"), list):
        audit = decision_audit.refresh_audit_payload(audit)
    available_horizons = [horizon for horizon in horizons if horizon in (audit.get("horizons") or [])]
    return {
        "available": True,
        "threshold_pct": audit.get("threshold_pct"),
        "horizons": {
            horizon: _horizon_payload(audit, horizon, top_n=top_n)
            for horizon in available_horizons
        },
    }
