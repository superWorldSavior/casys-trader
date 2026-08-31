"""Evaluate persisted indicator watches against fetched market bars."""

from __future__ import annotations

import math
import operator
from datetime import datetime
from typing import Callable

from trader.domain.market.features import build_indicator_snapshot, compute_indicator_values
from trader.domain.semantic.catalog import family_for_symbol

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


def _latest_bar_ts(bars: list[object]) -> str | None:
    try:
        latest = bars[-1]
        raw = latest.get("ts") if isinstance(latest, dict) else getattr(latest, "ts")
    except (AttributeError, IndexError):
        return None
    text = str(raw or "").strip()
    return text or None


def _evaluate_condition(
    condition: dict,
    bars: list[object],
    bars_by_key: dict[tuple[str, str], list[object]],
) -> dict:
    condition_type = str(condition.get("type") or "indicator").lower()
    indicator = condition.get("indicator")
    actual: float | None
    if condition_type == "close":
        try:
            latest = bars[-1]
            raw_close = latest["close"] if isinstance(latest, dict) else getattr(latest, "close")
            candidate = float(raw_close)
            actual = candidate if math.isfinite(candidate) else None
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            actual = None
    elif condition_type == "indicator" and isinstance(indicator, str):
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
    else:
        actual = None
    try:
        threshold = float(condition["value"])
        op = str(condition["op"])
        matched = math.isfinite(threshold) and _compare(actual, op, threshold)
    except (KeyError, TypeError, ValueError):
        threshold = None
        op = str(condition.get("op") or "")
        matched = False
    interval = str(condition.get("interval") or "1h")
    dependencies: set[str] = set()
    condition_symbol = str(condition.get("symbol") or "")
    if condition_type == "indicator" and indicator in _CROSS_ASSET_INDICATORS:
        family = family_for_symbol(condition_symbol)
        dependency_rows = (
            (symbol, symbol_bars)
            for (symbol, key_interval), symbol_bars in bars_by_key.items()
            if key_interval == interval
            and symbol_bars
            and family_for_symbol(symbol) == family
        )
    else:
        dependency_rows = ((condition_symbol, bars),)
    for symbol, dependency_bars in dependency_rows:
        if latest_ts := _latest_bar_ts(dependency_bars):
            dependencies.add(f"{symbol}:{interval}:{latest_ts}")

    result = {
        "symbol": condition["symbol"],
        "op": op,
        "value": threshold,
        "actual": actual,
        "interval": condition.get("interval"),
        "window": condition.get("window"),
        "matched": matched,
        "closed_bar_ts": _latest_bar_ts(bars),
        "_closed_bar_dependencies": sorted(dependencies),
    }
    if condition_type == "close":
        result["type"] = "close"
    elif isinstance(indicator, str):
        result["indicator"] = indicator
    return result


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
        closed_bar_parts = sorted(
            {
                dependency
                for item in evaluations
                for dependency in item.get("_closed_bar_dependencies", [])
            }
        )
        closed_bar_key = "|".join(closed_bar_parts) if closed_bar_parts else None
        if (
            closed_bar_key is not None
            and str(watch.get("last_triggered_bar_key") or "") == closed_bar_key
        ):
            continue
        event = {
            "watch_id": watch["id"],
            "symbol": watch["symbol"],
            "logic": logic,
            "on_trigger": _normalize_on_trigger(watch.get("on_trigger")),
            "matched": [
                {
                    key: value
                    for key, value in item.items()
                    if key != "_closed_bar_dependencies"
                }
                for item in evaluations
            ],
        }
        if closed_bar_key is not None:
            event["closed_bar_key"] = closed_bar_key
        if "order" in watch:
            event["order"] = watch["order"]
        if watch.get("rationale"):
            event["rationale"] = watch["rationale"]
        triggered.append(event)
    return triggered
