"""Indicator watches persisted by the agent and evaluated by the daemon."""

from __future__ import annotations

import hashlib
import json
import math
import operator
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

from .features import DEFAULT_INDICATORS, build_indicator_snapshot, compute_indicator_values
from .semantic.catalog import FAMILIES, INDICATOR_LABEL_VALUES, family_for_symbol, label_to_value, normalize_temporal_query

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
_ON_TRIGGERS = {"WAKE", "WAKE_WITH_ORDER_INTENT", "EXECUTE_ORDER"}
_CROSS_ASSET_INDICATORS = {"relative_strength", "spread_zscore"}

WATCH_REJECT_NOT_MAPPING = "not_a_mapping"
WATCH_REJECT_UNKNOWN_INDICATOR = "unknown_indicator"
WATCH_REJECT_INVALID_OPERATOR = "invalid_operator"
WATCH_REJECT_MISSING_THRESHOLD = "missing_threshold"
WATCH_REJECT_INVALID_ARMED_ORDER = "invalid_armed_order"
WATCH_REJECT_NON_FINITE_THRESHOLD = "non_finite_threshold"
WATCH_REJECT_UNKNOWN_LABEL = "unknown_indicator_label"

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


def _normalize_threshold(
    indicator: str, op: str, *values: object
) -> tuple[float | None, str, object]:
    """Parse threshold from raw values, resolving label strings via INDICATOR_LABEL_VALUES.

    Returns (threshold, reject_reason, raw_value):
    - threshold: parsed finite float, or None if invalid
    - reject_reason: empty string on success, else a WATCH_REJECT_* constant
    - raw_value: the first non-None raw value encountered (for error context)
    """
    raw_value: object = None
    unknown_label: object = None

    for value in values:
        if value is None:
            continue
        if raw_value is None:
            raw_value = value
        try:
            parsed = float(value)  # type: ignore[arg-type]
            if math.isfinite(parsed):
                # Ops abs* comparent abs(actual) au seuil : un seuil négatif rendrait
                # le prédicat permissif (abs>= -0.5 toujours vrai). On prend la magnitude,
                # comme pour les labels négatifs, pour rester cohérent et non-permissif.
                return (abs(parsed) if op in _ABS_OPS else parsed, "", None)
        except (TypeError, ValueError):
            pass
        if isinstance(value, str):
            lbl = label_to_value(indicator, value)
            if lbl is not None:
                resolved = abs(lbl) if op in _ABS_OPS else lbl
                return (resolved, "", None)
            unknown_label = value

    if unknown_label is not None and indicator in INDICATOR_LABEL_VALUES:
        return (None, WATCH_REJECT_UNKNOWN_LABEL, unknown_label)
    return (None, WATCH_REJECT_NON_FINITE_THRESHOLD, raw_value)


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
    threshold, reject_reason, raw_threshold = _normalize_threshold(
        indicator, op, raw.get("value"), raw.get("threshold")
    )
    if threshold is None:
        rejection: dict = {
            "reason": reject_reason,
            "indicator": indicator or None,
            "raw_value": _json_safe_scalar(raw_threshold),
        }
        if reject_reason == WATCH_REJECT_UNKNOWN_LABEL:
            rejection["valid_labels"] = sorted(INDICATOR_LABEL_VALUES.get(indicator, {}).keys())
        return (None, rejection)
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




# --- D7 étage B : plans armés (EXECUTE_ORDER) -------------------------------
# Le LLM arme un scénario d'entrée complet ; le daemon exécute au déclenchement
# SANS re-appel modèle. Contrat strict à l'armement (fast-fail) : intent
# OPEN_LONG/OPEN_SHORT, qty > 0, confidence 0..1 explicite, hard_stop spécifié.

_ARMABLE_INTENTS = {"OPEN_LONG": "BUY", "OPEN_SHORT": "SELL"}


def is_armed_plan(watch: dict) -> bool:
    """Prédicat UNIQUE « plan armé » (source de vérité pour daemon/scheduler/tui)."""
    return str(watch.get("on_trigger")) == "EXECUTE_ORDER" and isinstance(watch.get("order"), dict)


def _summarize_watch_conditions(conditions: object) -> list[dict]:
    if not isinstance(conditions, list):
        return []
    summaries: list[dict] = []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        summary = {
            "indicator": condition.get("indicator"),
            "op": condition.get("op"),
            "value": condition.get("value"),
            "timeframe": condition.get("timeframe") or condition.get("interval"),
        }
        summaries.append({key: value for key, value in summary.items() if value is not None})
    return summaries


def summarize_watch(watch: dict) -> dict:
    """Résumé compact et déterministe d'une indicator_watch pour l'agent."""
    armed = is_armed_plan(watch)
    summary = {}
    if watch.get("id") is not None:
        summary["id"] = watch.get("id")
    summary["kind"] = "armed" if armed else "wake"
    if armed:
        order = watch.get("order")
        if isinstance(order, dict) and order.get("intent") is not None:
            summary["intent"] = order.get("intent")
    if watch.get("expires_at") is not None:
        summary["expires_at"] = watch.get("expires_at")
    if watch.get("logic") is not None:
        summary["logic"] = watch.get("logic")
    summary["conditions"] = _summarize_watch_conditions(watch.get("conditions"))
    return summary


# Cap TTL des plans armés = 240 min, aligné sur la revue périodique garantie du
# gate (4 h) : l'expiration de watch étant SILENCIEUSE, un TTL plus court
# forcerait des réveils de ré-armement ou laisserait des trous désarmés. La
# fraîcheur est protégée par les checks au DÉCLENCHEMENT (stop franchi,
# position existante, stale), pas par l'horloge.
ARMED_ORDER_MAX_TTL_MINUTES = 240.0


def _armed_hard_stop_price(exit_plan: object) -> float | None:
    """Prix du hard_stop via le normaliseur commun (mêmes alias qu'à l'exécution :
    stop_loss/stop/sl) — pas de parsing parallèle qui divergerait en silence."""
    if not isinstance(exit_plan, dict):
        return None
    from .trade_plan import InvalidExitPlanError, normalize_exit_plan

    try:
        normalized = normalize_exit_plan(exit_plan)
    except InvalidExitPlanError:
        return None
    if not normalized:
        return None
    hard_stop = normalized.get("hard_stop")
    if isinstance(hard_stop, dict):
        if hard_stop.get("type", "price") != "price":
            return None
        raw = hard_stop.get("price")
    else:
        raw = hard_stop
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _armed_hard_stop_is_specified(exit_plan: object) -> bool:
    if not isinstance(exit_plan, dict):
        return False
    sentinel = object()
    hard_stop = exit_plan.get("hard_stop", sentinel)
    if hard_stop is sentinel:
        for alias in ("stop_loss", "stop", "sl"):
            if alias in exit_plan:
                hard_stop = exit_plan[alias]
                break
    if hard_stop is sentinel:
        return False
    if not isinstance(hard_stop, dict):
        try:
            price = float(hard_stop)
        except (TypeError, ValueError):
            return False
        return math.isfinite(price) and price > 0
    hard_stop_type = str(hard_stop.get("type", "price"))
    if hard_stop_type == "price":
        try:
            price = float(hard_stop.get("price"))
        except (TypeError, ValueError):
            return False
        return math.isfinite(price) and price > 0
    if hard_stop_type == "percent":
        return hard_stop.get("percent") is not None
    if hard_stop_type == "volatility_multiple":
        return hard_stop.get("multiple") is not None
    if hard_stop_type == "structural":
        return hard_stop.get("anchor") is not None
    return False


def normalize_armed_order(raw: object) -> dict | None:
    """Ordre armable ou None. L'action est DÉRIVÉE de l'intent (pas de mismatch)."""
    if not isinstance(raw, dict):
        return None
    intent = str(raw.get("intent") or "").upper()
    action = _ARMABLE_INTENTS.get(intent)
    if action is None:
        return None
    try:
        qty = float(raw.get("qty") if raw.get("qty") is not None else raw.get("quantity"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(qty) or qty <= 0:
        return None
    exit_plan = raw.get("exit_plan")
    if not _armed_hard_stop_is_specified(exit_plan):
        return None
    from .trade_plan import InvalidExitPlanError, validate_exit_plan

    try:
        validate_exit_plan(exit_plan, allow_unresolved=True)
    except InvalidExitPlanError:
        return None
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    order = {
        "intent": intent,
        "action": action,
        "qty": qty,
        "confidence": confidence,
        "exit_plan": exit_plan,
    }
    if raw.get("rationale"):
        order["rationale"] = str(raw["rationale"])[:500]
    return order


def armed_order_price_coherent(order: dict, *, price: float) -> bool:
    """Le prix au déclenchement est-il du bon côté du hard_stop ?

    Un plan armé peut se déclencher loin du prix imaginé à l'armement ; si le
    prix a déjà franchi le stop, ouvrir = position instantanément stoppable.
    """
    stop = _armed_hard_stop_price(order.get("exit_plan"))
    if stop is None or not math.isfinite(price):
        return False
    if order.get("intent") == "OPEN_LONG":
        return price > stop
    return price < stop

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
    # Filet de sécurité : watches ATOMIQUES. Une seule condition rejetée invalide
    # toute la veille — on ne persiste jamais une watch amputée (sinon un logic=all
    # ou un EXECUTE_ORDER privé d'un prédicat se déclencherait sur la condition
    # résiduelle, souvent permissive → réveil/exécution fantôme).
    if rejections:
        return IndicatorWatchResult(None, rejections)

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
    if on_trigger == "EXECUTE_ORDER":
        armed = normalize_armed_order(order)
        if armed is None:
            # contrat d'armement non rempli -> on garde la veille mais l'ordre
            # repassera par le LLM (jamais d'exécution directe non validée)
            rejections.append({"reason": WATCH_REJECT_INVALID_ARMED_ORDER})
            on_trigger = "WAKE_WITH_ORDER_INTENT"
        else:
            order = armed
            ttl = min(ttl, ARMED_ORDER_MAX_TTL_MINUTES)
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
    """Normalize an LLM-provided watch into a small persistent JSON shape.

    Returns the watch dict on success, or None if any condition was rejected
    (atomicity guarantee: a partial watch is never returned).
    NOTE: cette fonction ne remonte PAS les rejets — pour le feedback structuré
    (raison du rejet, labels valides…), utiliser `build_indicator_watch` qui
    retourne un `IndicatorWatchResult` avec `.rejections`.
    """
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
