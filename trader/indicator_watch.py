"""Indicator watches persisted by the agent and evaluated by the daemon."""

from __future__ import annotations

import hashlib
import json
import math
import operator
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

from .features import DEFAULT_INDICATORS, build_indicator_snapshot, compute_indicator_values
from .semantic.catalog import FAMILIES, family_for_symbol, normalize_temporal_query

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

_ON_TRIGGER_ALIASES = {
    "ORDER": "WAKE_WITH_ORDER_INTENT",
}
_ON_TRIGGERS = {"WAKE", "WAKE_WITH_ORDER_INTENT"}
_CROSS_ASSET_INDICATORS = {"relative_strength", "spread_zscore"}

WATCH_REJECT_NOT_MAPPING = "not_a_mapping"
WATCH_REJECT_UNKNOWN_INDICATOR = "unknown_indicator"
WATCH_REJECT_INVALID_OPERATOR = "invalid_operator"
WATCH_REJECT_MISSING_THRESHOLD = "missing_threshold"
WATCH_REJECT_NON_FINITE_THRESHOLD = "non_finite_threshold"

_ABS_OPS: dict[str, Callable[[float, float], bool]] = {
    "abs>": operator.gt,
    "abs>=": operator.ge,
    "abs<": operator.lt,
    "abs<=": operator.le,
}

# Vocabulaire d'opérateurs accepté par le validateur, dérivé des tables ci-dessus
# (source de vérité unique). Le prompt s'en sert pour ne pas proposer à l'agent
# une notation qui sera rejetée (cf rejet réel `op="eq"`).
WATCH_VALID_OPERATORS: tuple[str, ...] = tuple(_OPS) + tuple(_ABS_OPS)


class IndicatorWatchResult(NamedTuple):
    watch: dict | None
    rejections: list[dict]


def _parse_dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except Exception:
        parsed = default
    return min(max(parsed, minimum), maximum)


def _bounded_float(value: object, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except Exception:
        parsed = default
    return min(max(parsed, minimum), maximum)


def _optional_finite_float(*values: object) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def _json_safe_scalar(value: object) -> object:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def _normalize_on_trigger(raw: object) -> str:
    on_trigger = str(raw or "WAKE").upper()
    on_trigger = _ON_TRIGGER_ALIASES.get(on_trigger, on_trigger)
    return on_trigger if on_trigger in _ON_TRIGGERS else "WAKE"


def _condition_from_raw(raw: object, *, owner_symbol: str) -> tuple[dict | None, dict | None]:
    if not isinstance(raw, dict):
        return (
            None,
            {"reason": WATCH_REJECT_NOT_MAPPING, "indicator": None, "raw_value": None},
        )
    indicator = str(raw.get("indicator") or raw.get("name") or "")
    if indicator not in DEFAULT_INDICATORS:
        return (
            None,
            {
                "reason": WATCH_REJECT_UNKNOWN_INDICATOR,
                "indicator": indicator or None,
                "raw_value": None,
            },
        )
    op = str(raw.get("op") or raw.get("operator") or ">=").lower()
    if op not in _OPS and op not in _ABS_OPS:
        return (
            None,
            {
                "reason": WATCH_REJECT_INVALID_OPERATOR,
                "indicator": indicator or None,
                "raw_value": op,
            },
        )
    if "value" not in raw and "threshold" not in raw:
        return (
            None,
            {
                "reason": WATCH_REJECT_MISSING_THRESHOLD,
                "indicator": indicator or None,
                "raw_value": None,
            },
        )
    threshold = _optional_finite_float(raw.get("value"), raw.get("threshold"))
    if threshold is None:
        return (
            None,
            {
                "reason": WATCH_REJECT_NON_FINITE_THRESHOLD,
                "indicator": indicator or None,
                "raw_value": _json_safe_scalar(raw.get("value") if "value" in raw else raw.get("threshold")),
            },
        )
    temporal = normalize_temporal_query(
        timeframe=str(raw.get("interval") or raw.get("timeframe") or "1h"),
        lookback=None if raw.get("lookback") is None else str(raw["lookback"]),
        window=_bounded_int(raw.get("window"), default=48, minimum=2, maximum=240),
        as_of=None if raw.get("as_of") is None else str(raw["as_of"]),
    )
    return (
        {
            "symbol": str(raw.get("symbol") or owner_symbol),
            "indicator": indicator,
            "op": op,
            "value": threshold,
            "interval": temporal["timeframe"],
            "timeframe": temporal["timeframe"],
            "source_interval": temporal["source_interval"],
            "lookback": temporal["lookback"],
            "window": temporal["window"],
            "as_of": temporal["as_of"],
        },
        None,
    )


def build_indicator_watch(
    raw: object,
    *,
    owner_symbol: str,
    now: datetime,
    max_ttl_minutes: float = 24 * 60,
) -> IndicatorWatchResult:
    """Normalize an LLM-provided watch into a small persistent JSON shape."""
    if not isinstance(raw, dict):
        return IndicatorWatchResult(None, [])
    raw_conditions = raw.get("conditions") or raw.get("when") or []
    if isinstance(raw_conditions, dict):
        raw_conditions = [raw_conditions]
    conditions = []
    rejections = []
    for item in list(raw_conditions):
        condition, rejection = _condition_from_raw(item, owner_symbol=owner_symbol)
        if condition is not None:
            conditions.append(condition)
        elif rejection is not None:
            rejections.append(rejection)
    if not conditions:
        return IndicatorWatchResult(None, rejections)

    ttl = _bounded_float(
        raw.get("ttl_minutes", raw.get("duration_minutes", raw.get("expires_in_minutes"))),
        default=60.0,
        minimum=1.0,
        maximum=max_ttl_minutes,
    )
    logic = str(raw.get("logic") or "all").lower()
    if logic not in {"all", "any"}:
        logic = "all"
    on_trigger = _normalize_on_trigger(raw.get("on_trigger") or raw.get("trigger"))
    order = raw.get("order")
    created_at = now.astimezone(timezone.utc).isoformat()
    expires_at = (now + timedelta(minutes=ttl)).astimezone(timezone.utc).isoformat()
    id_payload = {
        "symbol": owner_symbol,
        "created_at": created_at,
        "logic": logic,
        "conditions": conditions,
        "on_trigger": on_trigger,
    }
    watch_id = hashlib.sha256(json.dumps(id_payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    watch = {
        "id": f"{owner_symbol}:{watch_id}",
        "symbol": owner_symbol,
        "created_at": created_at,
        "expires_at": expires_at,
        "logic": logic,
        "on_trigger": on_trigger,
        "conditions": conditions,
    }
    if isinstance(order, dict):
        watch["order"] = order
    if raw.get("rationale"):
        watch["rationale"] = str(raw["rationale"])
    return IndicatorWatchResult(watch, rejections)


def normalize_indicator_watch(
    raw: object,
    *,
    owner_symbol: str,
    now: datetime,
    max_ttl_minutes: float = 24 * 60,
) -> dict | None:
    """Normalize an LLM-provided watch into a small persistent JSON shape."""
    return build_indicator_watch(raw, owner_symbol=owner_symbol, now=now, max_ttl_minutes=max_ttl_minutes).watch


def _same_family_symbols(symbol: str, universe_symbols: list[str] | None) -> list[str]:
    family = family_for_symbol(symbol)
    if family is None:
        return []
    family_symbols = FAMILIES.get(family, [])
    if universe_symbols is not None:
        allowed = set(universe_symbols)
        family_symbols = [candidate for candidate in family_symbols if candidate in allowed]
    return family_symbols


def watch_market_requests(
    watches: list[dict],
    *,
    universe_symbols: list[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Return unique `(symbol, interval, lookback)` requests required by watches."""
    seen: set[tuple[str, str, str]] = set()
    requests: list[tuple[str, str, str]] = []

    def add(symbol: str, interval: str, lookback: str) -> None:
        item = (symbol, interval, lookback)
        if item in seen:
            return
        seen.add(item)
        requests.append(item)

    for watch in watches:
        for condition in watch.get("conditions", []):
            symbol = str(condition.get("symbol"))
            interval = str(condition.get("interval") or "1h")
            lookback = str(condition.get("lookback") or "5d")
            add(symbol, interval, lookback)
            if condition.get("indicator") in _CROSS_ASSET_INDICATORS:
                for peer in _same_family_symbols(symbol, universe_symbols):
                    add(peer, interval, lookback)
    return requests


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
        is_triggered = any(item["matched"] for item in evaluations) if logic == "any" else all(item["matched"] for item in evaluations)
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
