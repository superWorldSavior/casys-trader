"""Evaluate persisted indicator watches against fetched market bars."""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Callable

from trader.domain.market.features import build_indicator_snapshot, compute_indicator_values

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

_ABS_OPS: dict[str, Callable[[float, float], bool]] = {
    "abs>": operator.gt,
    "abs>=": operator.ge,
    "abs<": operator.lt,
    "abs<=": operator.le,
}

_ON_TRIGGER_ALIASES = {
    "ORDER": "WAKE_WITH_ORDER_INTENT",
}
_ON_TRIGGERS = {"WAKE", "WAKE_WITH_ORDER_INTENT", "EXECUTE_ORDER"}
_CROSS_ASSET_INDICATORS = {"relative_strength", "spread_zscore"}


def _parse_dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _normalize_on_trigger(raw: object) -> str:
    on_trigger = str(raw or "WAKE").upper()
    on_trigger = _ON_TRIGGER_ALIASES.get(on_trigger, on_trigger)
    return on_trigger if on_trigger in _ON_TRIGGERS else "WAKE"


def _compare(actual: float | None, op: str, threshold: float) -> bool:
    if actual is None:
        return False
    if op in _ABS_OPS:
        return _ABS_OPS[op](abs(actual), threshold)
    return _OPS[op](actual, threshold)


def _evaluate_condition(
    condition: dict,
    bars: list[object],
    bars_by_key: dict[tuple[str, str], list[object]],
) -> dict:
    indicator = str(condition["indicator"])
    try:
        window = int(condition.get("window") or 48)
        if indicator in _CROSS_ASSET_INDICATORS:
            interval = str(condition.get("interval") or "1h")
            bars_by_symbol = {
                symbol: symbol_bars
                for (symbol, key_interval), symbol_bars in bars_by_key.items()
                if key_interval == interval
            }
            snapshot = build_indicator_snapshot(
                bars_by_symbol,
                symbols=list(bars_by_symbol),
                names=[indicator],
                window=window,
            )
            values = snapshot.get(str(condition["symbol"]), {"indicators": {}})["indicators"]
        else:
            values = compute_indicator_values(
                bars,
                names=[indicator],
                window=window,
            )
    except Exception:
        actual = None
    else:
        actual = values.get(indicator)
    threshold = float(condition["value"])
    op = str(condition["op"])
    matched = _compare(actual, op, threshold)
    return {
        "symbol": condition["symbol"],
        "indicator": indicator,
        "op": op,
        "value": threshold,
        "actual": actual,
        "interval": condition.get("interval"),
        "window": condition.get("window"),
        "matched": matched,
    }


def evaluate_indicator_watches(
    watches: list[dict],
    bars_by_key: dict[tuple[str, str], list[object]],
    *,
    now: datetime,
) -> list[dict]:
    """Evaluate watches against already fetched market bars."""
    triggered: list[dict] = []
    for watch in watches:
        expires_at = _parse_dt(watch.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        evaluations = []
        for condition in watch.get("conditions", []):
            key = (str(condition.get("symbol")), str(condition.get("interval") or "1h"))
            evaluations.append(_evaluate_condition(condition, bars_by_key.get(key, []), bars_by_key))
        if not evaluations:
            continue
        logic = str(watch.get("logic") or "all").lower()
        is_triggered = (
            any(item["matched"] for item in evaluations)
            if logic == "any"
            else all(item["matched"] for item in evaluations)
        )
        if not is_triggered:
            continue
        event = {
            "watch_id": watch["id"],
            "symbol": watch["symbol"],
            "logic": logic,
            "on_trigger": _normalize_on_trigger(watch.get("on_trigger")),
            "matched": evaluations,
        }
        if "order" in watch:
            event["order"] = watch["order"]
        if watch.get("rationale"):
            event["rationale"] = watch["rationale"]
        triggered.append(event)
    return triggered
