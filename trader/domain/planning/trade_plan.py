"""Persisted trade plans defined by the agent and enforced by the daemon."""

from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta, timezone

from trader.domain.planning.exit_plan_spec import (
    STRUCTURAL_HARD_STOP_ANCHORS as STRUCTURAL_HARD_STOP_ANCHORS,
    InvalidExitPlanError,
    StructuralHardStopAnchor,
    _bounded_float,
    _bounded_fraction,
    _positive_float,
    _positive_int,
    normalize_exit_plan,
    validate_exit_plan as _validate_exit_plan,
)
from trader.domain.planning.indicator_watch import normalize_indicator_watch
from trader.domain.planning import exit_plan_spec as _domain_exit_plan_spec
from trader.domain.trade_plan import (
    TRAILING_STOP_TRAIL_TYPES,
    MoveStopTo as MoveStopTo,
    PositionSide,
    ProfitProtection,
    TakeProfit,
    TradePlan,
    TrailingStop,
    TrailingStopTrailType as TrailingStopTrailType,
)
from trader.domain.market.features import swing_high, swing_low, vwap

STRUCTURAL_HARD_STOP_LEVELS: dict[StructuralHardStopAnchor, object] = {
    "swing_low": swing_low,
    "swing_high": swing_high,
    "vwap": vwap,
}


def _parse_price(raw: object) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return float(raw)
    if raw.get("type", "price") != "price":
        return None
    price = raw.get("price")
    return None if price is None else float(price)


def validate_exit_plan(
    raw_exit_plan: dict | None,
    *,
    reference_volatility: float | None = None,
    allow_unresolved: bool = False,
) -> None:
    # Propage un éventuel monkeypatch de TRAILING_STOP_TRAIL_TYPES (tests qui patchent
    # ce module) vers le module domaine qui exécute réellement la validation.
    _domain_exit_plan_spec.TRAILING_STOP_TRAIL_TYPES = TRAILING_STOP_TRAIL_TYPES
    return _validate_exit_plan(
        raw_exit_plan,
        reference_volatility=reference_volatility,
        allow_unresolved=allow_unresolved,
    )


def _distance_pct_warning(
    *,
    code: str,
    field: str,
    distance: float,
    entry_price: float,
    limit_pct: float,
    limit_distance: float,
) -> dict:
    return {
        "code": code,
        "field": field,
        "distance": distance,
        "distance_pct": distance / entry_price,
        "limit_distance": limit_distance,
        "limit_pct": limit_pct,
    }


def _distance_pct_bound_warnings(
    *,
    distance: float,
    entry_price: float,
    raw: dict,
) -> list[dict]:
    warnings: list[dict] = []
    tolerance = max(abs(distance), abs(entry_price), 1.0) * 1e-12
    if raw.get("min_pct") is not None:
        min_pct = _bounded_fraction(raw["min_pct"], "hard_stop_min_pct")
        min_distance = min_pct * entry_price
        if distance + tolerance < min_distance:
            warnings.append(
                _distance_pct_warning(
                    code="hard_stop_below_min_pct",
                    field="min_pct",
                    distance=distance,
                    entry_price=entry_price,
                    limit_pct=min_pct,
                    limit_distance=min_distance,
                )
            )
    if raw.get("max_pct") is not None:
        max_pct = _bounded_fraction(raw["max_pct"], "hard_stop_max_pct")
        max_distance = max_pct * entry_price
        if distance - tolerance > max_distance:
            warnings.append(
                _distance_pct_warning(
                    code="hard_stop_above_max_pct",
                    field="max_pct",
                    distance=distance,
                    entry_price=entry_price,
                    limit_pct=max_pct,
                    limit_distance=max_distance,
                )
            )
    return warnings


def _attach_trace_warnings(trace: dict, warnings: list[dict]) -> None:
    if warnings:
        trace["warnings"] = warnings


def _copy_trace_fields(trace: dict, raw: dict, fields: tuple[str, ...]) -> None:
    for key in fields:
        if raw.get(key) is not None:
            trace[key] = raw[key]


def resolve_exit_plan(
    raw_exit_plan: dict | None,
    *,
    entry_price: float,
    side: PositionSide,
    reference_volatility: float | None = None,
    bars: list | None = None,
    protective_reference_price: float | None = None,
) -> tuple[dict | None, dict]:
    if raw_exit_plan is None:
        return None, {}
    raw_exit_plan = normalize_exit_plan(raw_exit_plan)

    entry_price_value = _positive_float(entry_price, "entry_price")
    protective_reference = (
        None
        if protective_reference_price is None
        else _positive_float(protective_reference_price, "protective_reference_price")
    )
    if side not in {"LONG", "SHORT"}:
        raise InvalidExitPlanError("side_unsupported")

    validate_exit_plan(
        raw_exit_plan,
        reference_volatility=reference_volatility,
        allow_unresolved=True,
    )

    resolved = copy.deepcopy(raw_exit_plan)
    trace: dict = {}
    stop_distance: float | None = None

    hard_stop = resolved.get("hard_stop")
    if hard_stop is not None:
        if isinstance(hard_stop, dict):
            hard_stop_type = str(hard_stop.get("type", "price"))
            if hard_stop_type == "price":
                resolved_stop = float(hard_stop["price"])
                stop_distance = abs(entry_price_value - resolved_stop)
                trace["hard_stop"] = {
                    "spec_type": "price",
                    "distance": stop_distance,
                    "resolved_price": resolved_stop,
                }
            elif hard_stop_type == "percent":
                percent = _bounded_fraction(hard_stop.get("percent"), "hard_stop_percent")
                distance = entry_price_value * percent
                warnings = _distance_pct_bound_warnings(
                    distance=distance,
                    entry_price=entry_price_value,
                    raw=hard_stop,
                )
                resolved_stop = (
                    entry_price_value - distance
                    if side == "LONG"
                    else entry_price_value + distance
                )
                if resolved_stop <= 0:
                    raise InvalidExitPlanError("hard_stop_resolved_non_positive")
                resolved["hard_stop"] = {"type": "price", "price": resolved_stop}
                stop_distance = distance
                trace["hard_stop"] = {
                    "spec_type": "percent",
                    "percent": percent,
                    "distance": distance,
                    "clamped": False,
                    "resolved_price": resolved_stop,
                }
                _copy_trace_fields(trace["hard_stop"], hard_stop, ("min_pct", "max_pct"))
                _attach_trace_warnings(trace["hard_stop"], warnings)
            elif hard_stop_type == "volatility_multiple":
                if reference_volatility is None:
                    raise InvalidExitPlanError("hard_stop_volatility_unavailable")
                volatility = _positive_float(reference_volatility, "reference_volatility")
                multiple = _positive_float(hard_stop.get("multiple"), "hard_stop_multiple")
                distance = volatility * multiple
                warnings = _distance_pct_bound_warnings(
                    distance=distance,
                    entry_price=entry_price_value,
                    raw=hard_stop,
                )
                resolved_stop = (
                    entry_price_value - distance
                    if side == "LONG"
                    else entry_price_value + distance
                )
                if resolved_stop <= 0:
                    raise InvalidExitPlanError("hard_stop_resolved_non_positive")
                resolved["hard_stop"] = {"type": "price", "price": resolved_stop}
                stop_distance = distance
                trace["hard_stop"] = {
                    "spec_type": "volatility_multiple",
                    "multiple": multiple,
                    "reference_volatility": volatility,
                    "distance": distance,
                    "clamped": False,
                    "resolved_price": resolved_stop,
                }
                _copy_trace_fields(
                    trace["hard_stop"],
                    hard_stop,
                    ("source", "timeframe", "window", "min_pct", "max_pct"),
                )
                _attach_trace_warnings(trace["hard_stop"], warnings)
            elif hard_stop_type == "structural":
                if not bars:
                    raise InvalidExitPlanError("hard_stop_bars_unavailable")
                anchor = str(hard_stop["anchor"])
                window = _positive_int(hard_stop.get("window"), "hard_stop_window")
                level = STRUCTURAL_HARD_STOP_LEVELS[anchor](bars, window)
                if level is None:
                    raise InvalidExitPlanError("hard_stop_level_unavailable")
                level_value = float(level)
                if protective_reference is None and (
                    (side == "LONG" and level_value >= entry_price_value)
                    or (side == "SHORT" and level_value <= entry_price_value)
                ):
                    raise InvalidExitPlanError("hard_stop_structural_wrong_side")
                volatility: float | None = None
                if hard_stop.get("buffer_pct") is not None:
                    buffer = _positive_float(hard_stop["buffer_pct"], "hard_stop_buffer_pct")
                    buffer *= entry_price_value
                elif hard_stop.get("buffer_atr") is not None:
                    if reference_volatility is None:
                        raise InvalidExitPlanError("hard_stop_buffer_volatility_unavailable")
                    volatility = _positive_float(reference_volatility, "reference_volatility")
                    buffer = _positive_float(hard_stop["buffer_atr"], "hard_stop_buffer_atr")
                    buffer *= volatility
                else:
                    buffer = 0.0

                raw_stop = level_value - buffer if side == "LONG" else level_value + buffer
                side_reference = (
                    entry_price_value if protective_reference is None else protective_reference
                )
                side_distance = (
                    side_reference - raw_stop
                    if side == "LONG"
                    else raw_stop - side_reference
                )
                if side_distance <= 0:
                    raise InvalidExitPlanError("hard_stop_structural_wrong_side")
                distance = abs(entry_price_value - raw_stop)
                initial_distance = (
                    entry_price_value - raw_stop
                    if side == "LONG"
                    else raw_stop - entry_price_value
                )
                if protective_reference is None and initial_distance <= 0:
                    raise InvalidExitPlanError("hard_stop_structural_wrong_side")
                warnings = _distance_pct_bound_warnings(
                    distance=distance,
                    entry_price=entry_price_value,
                    raw=hard_stop,
                )
                resolved_stop = raw_stop
                if resolved_stop <= 0:
                    raise InvalidExitPlanError("hard_stop_resolved_non_positive")
                resolved["hard_stop"] = {"type": "price", "price": resolved_stop}
                stop_distance = distance
                trace["hard_stop"] = {
                    "spec_type": "structural",
                    "anchor": anchor,
                    "window": window,
                    "level": level_value,
                    "buffer": buffer,
                    "distance": distance,
                    "clamped": False,
                    "resolved_price": resolved_stop,
                }
                if protective_reference is not None:
                    trace["hard_stop"]["protective_reference_price"] = protective_reference
                if volatility is not None:
                    trace["hard_stop"]["reference_volatility"] = volatility
                _copy_trace_fields(
                    trace["hard_stop"],
                    hard_stop,
                    ("min_pct", "max_pct"),
                )
                _attach_trace_warnings(trace["hard_stop"], warnings)
        else:
            resolved_stop = float(hard_stop)
            stop_distance = abs(entry_price_value - resolved_stop)
            resolved["hard_stop"] = {"type": "price", "price": resolved_stop}
            trace["hard_stop"] = {
                "spec_type": "price",
                "distance": stop_distance,
                "resolved_price": resolved_stop,
            }

    take_profit_traces: list[dict] = []
    if resolved.get("take_profits") is not None:
        take_profits = resolved.get("take_profits", []) or []
        for item in take_profits:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "price")) == "risk_multiple" and item.get("price") is None:
                if stop_distance is None:
                    raise InvalidExitPlanError("take_profit_risk_multiple_requires_stop")
                risk_multiple = _positive_float(item.get("r"), "take_profit_r")
                resolved_price = (
                    entry_price_value + risk_multiple * stop_distance
                    if side == "LONG"
                    else entry_price_value - risk_multiple * stop_distance
                )
                if resolved_price <= 0:
                    raise InvalidExitPlanError("take_profit_resolved_non_positive")
                item["type"] = "price"
                item["price"] = resolved_price
                take_profit_traces.append(
                    {
                        "spec_type": "risk_multiple",
                        "r": risk_multiple,
                        "resolved_price": resolved_price,
                    }
                )
            elif item.get("price") is not None:
                take_profit_traces.append(
                    {
                        "spec_type": "price",
                        "resolved_price": float(item["price"]),
                    }
                )
        trace["take_profits"] = take_profit_traces

    validate_exit_plan(resolved, reference_volatility=reference_volatility)
    return resolved, trace


def _side_from_order_side(side: str) -> PositionSide:
    return "LONG" if side == "BUY" else "SHORT"


def _plan_id(symbol: str, opened_at: str) -> str:
    safe_ts = opened_at.replace(":", "").replace("+", "Z")
    return f"{symbol}-{safe_ts}"


def _trail_amount_in_price_units(
    *,
    entry_price: float,
    reference_volatility: float | None,
    trail_type: str,
    trail_value: float,
) -> float | None:
    if trail_type == "price":
        return trail_value
    if trail_type == "percent":
        return entry_price * trail_value
    if trail_type == "volatility_multiple" and reference_volatility is not None:
        return reference_volatility * trail_value
    return None


def _trail_value_for_price_amount(
    *,
    entry_price: float,
    reference_volatility: float,
    trail_type: str,
    amount: float,
) -> float:
    if trail_type == "percent":
        return amount / entry_price
    if trail_type == "volatility_multiple":
        return amount / reference_volatility
    return amount


def create_trade_plan(
    *,
    symbol: str,
    side: PositionSide,
    quantity: float,
    entry_price: float,
    opened_at: str,
    raw_exit_plan: dict | None,
    reference_volatility: float | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_fallback_reason: str | None = None,
    llm_confidence: float | None = None,
) -> TradePlan:
    raw = normalize_exit_plan(raw_exit_plan) or {}
    try:
        entry_price_value = float(entry_price)
    except (TypeError, ValueError) as exc:
        raise InvalidExitPlanError("entry_price_must_be_number") from exc
    if not math.isfinite(entry_price_value):
        raise InvalidExitPlanError("entry_price_must_be_finite")
    if entry_price_value <= 0:
        raise InvalidExitPlanError("entry_price_must_be_positive")
    parsed_reference_volatility = (
        None if reference_volatility is None else _positive_float(reference_volatility, "reference_volatility")
    )
    validate_exit_plan(raw, reference_volatility=parsed_reference_volatility)
    max_hold_minutes = (
        None
        if raw.get("max_hold_minutes") is None
        else _positive_float(raw["max_hold_minutes"], "max_hold_minutes")
    )
    # A max hold is an executable lifecycle deadline, not merely a large float.
    # Check the actual calendar range up front so creating a watch can never
    # leak an implementation OverflowError from datetime/timedelta.
    _max_hold_deadline_from_opened_at(opened_at, max_hold_minutes)
    take_profits: list[TakeProfit] = []
    remaining_fraction = 1.0
    for index, item in enumerate(raw.get("take_profits", []) or []):
        if not isinstance(item, dict):
            continue
        fraction = float(item.get("fraction", remaining_fraction))
        fraction = max(0.0, min(fraction, remaining_fraction))
        remaining_fraction -= fraction
        take_profits.append(
            TakeProfit(
                name=str(item.get("name") or f"tp{index + 1}"),
                price=float(item["price"]),
                fraction=fraction,
                quantity=round(quantity * fraction, 8),
                after_fill=str(item.get("after_fill") or ""),
            )
        )

    trailing_raw = raw.get("trailing_stop")
    trailing_stop = None
    if isinstance(trailing_raw, dict):
        trail_type = str(trailing_raw.get("trail_type", "price"))
        if trail_type in TRAILING_STOP_TRAIL_TYPES:
            trail_value = float(trailing_raw["trail_value"])
            trail_floored = False
            if parsed_reference_volatility is not None:
                trail_amount = _trail_amount_in_price_units(
                    entry_price=entry_price_value,
                    reference_volatility=parsed_reference_volatility,
                    trail_type=trail_type,
                    trail_value=trail_value,
                )
                if trail_amount is not None and trail_amount < parsed_reference_volatility:
                    trail_value = _trail_value_for_price_amount(
                        entry_price=entry_price_value,
                        reference_volatility=parsed_reference_volatility,
                        trail_type=trail_type,
                        amount=parsed_reference_volatility,
                    )
                    trail_floored = True
            trailing_stop = TrailingStop(
                enabled_after=(
                    None
                    if trailing_raw.get("enabled_after") in (None, "")
                    else str(trailing_raw["enabled_after"])
                ),
                trail_type=trail_type,  # type: ignore[arg-type]
                trail_value=trail_value,
                trail_floored=trail_floored,
            )

    protection_raw = raw.get("profit_protection")
    profit_protection = _profit_protection_from_raw(protection_raw)
    exit_watch = _normalize_exit_watch(
        raw.get("exit_watch"),
        symbol=symbol,
        opened_at=opened_at,
        max_ttl_minutes=max_hold_minutes or float(24 * 60),
    )

    return TradePlan(
        id=_plan_id(symbol, opened_at),
        symbol=symbol,
        side=side,
        quantity=float(quantity),
        remaining_quantity=float(quantity),
        entry_price=entry_price_value,
        opened_at=opened_at,
        reference_volatility=parsed_reference_volatility,
        hard_stop_price=_parse_price(raw.get("hard_stop")),
        take_profits=take_profits,
        trailing_stop=trailing_stop,
        max_hold_minutes=max_hold_minutes,
        high_watermark=float(entry_price),
        low_watermark=float(entry_price),
        profit_protection=profit_protection,
        exit_watch=exit_watch,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_fallback_reason=llm_fallback_reason,
        llm_confidence=llm_confidence,
    )


def create_trade_plan_from_order(
    *,
    symbol: str,
    order_side: str,
    quantity: float,
    entry_price: float,
    opened_at: str,
    raw_exit_plan: dict | None,
    reference_volatility: float | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_fallback_reason: str | None = None,
    llm_confidence: float | None = None,
) -> TradePlan:
    return create_trade_plan(
        symbol=symbol,
        side=_side_from_order_side(order_side),
        quantity=quantity,
        entry_price=entry_price,
        opened_at=opened_at,
        raw_exit_plan=raw_exit_plan,
        reference_volatility=reference_volatility,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_fallback_reason=llm_fallback_reason,
        llm_confidence=llm_confidence,
    )


def _trailing_from_dict(raw: dict | None) -> TrailingStop | None:
    if raw is None:
        return None
    raw_trail_value = raw.get("trail_value")
    if raw_trail_value is None:
        return None
    trail_value = float(raw_trail_value)
    if not math.isfinite(trail_value):
        return None
    return TrailingStop(
        enabled_after=raw.get("enabled_after"),
        trail_type=raw["trail_type"],
        trail_value=trail_value,
        trail_floored=bool(raw.get("trail_floored", False)),
    )


def _profit_protection_from_raw(raw: dict | None) -> ProfitProtection | None:
    if raw is None:
        return None
    if raw.get("enabled", True) is False:
        return None
    move_stop_to = str(raw.get("move_stop_to", "breakeven"))
    if move_stop_to not in {"breakeven", "none"}:
        move_stop_to = "breakeven"
    return ProfitProtection(
        enabled=True,
        arm_at_r=float(raw.get("arm_at_r", 0.5)),
        trigger_on_giveback_pct=float(raw.get("trigger_on_giveback_pct", 0.4)),
        close_fraction=float(raw.get("close_fraction", 1.0 / 3.0)),
        move_stop_to=move_stop_to,  # type: ignore[arg-type]
        min_hold_minutes=float(raw.get("min_hold_minutes", 10.0)),
        lock_r=(None if raw.get("lock_r") is None else float(raw["lock_r"])),
        triggered=bool(raw.get("triggered", False)),
    )


def _normalize_exit_watch(
    raw: object,
    *,
    symbol: str,
    opened_at: str,
    max_ttl_minutes: float,
    now: datetime | None = None,
    preserve_last_triggered_at: bool = True,
) -> dict | None:
    if not isinstance(raw, dict):
        return None
    if now is None:
        try:
            created_at = datetime.fromisoformat(opened_at)
        except ValueError:
            created_at = datetime.now(timezone.utc)
    else:
        created_at = now
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)
    watch = normalize_indicator_watch(
        raw,
        owner_symbol=symbol,
        now=created_at,
        max_ttl_minutes=max_ttl_minutes,
    )
    # None peut venir d'une condition rejetée (silencieux ici par choix : exit_watch
    # est optionnel ; pour le feedback structuré utiliser build_indicator_watch qui
    # retourne IndicatorWatchResult.rejections).
    if watch is None:
        return None
    watch["on_trigger"] = "WAKE"
    watch["source"] = "exit_watch"
    watch["cooldown_minutes"] = _bounded_float(
        raw.get("cooldown_minutes"),
        default=15.0,
        minimum=1.0,
        maximum=24 * 60.0,
    )
    if preserve_last_triggered_at and raw.get("last_triggered_at"):
        watch["last_triggered_at"] = str(raw["last_triggered_at"])
    if preserve_last_triggered_at and raw.get("last_triggered_bar_key"):
        watch["last_triggered_bar_key"] = str(raw["last_triggered_bar_key"])
    return watch


def _max_hold_deadline_from_opened_at(
    opened_at: str,
    max_hold_minutes: float | None,
) -> datetime | None:
    """Build a representable max-hold deadline or raise a domain error."""
    if max_hold_minutes is None:
        return None
    try:
        parsed = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise InvalidExitPlanError("max_hold_opened_at_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    try:
        return parsed + timedelta(minutes=max_hold_minutes)
    except OverflowError as exc:
        raise InvalidExitPlanError("max_hold_minutes_out_of_range") from exc


def _effective_max_hold_minutes(
    plan: TradePlan,
    update: dict,
    resolved: dict,
) -> float | None:
    raw = (
        resolved.get("max_hold_minutes")
        if "max_hold_minutes" in update
        else plan.max_hold_minutes
    )
    return None if raw is None else _positive_float(raw, "max_hold_minutes")


def _max_hold_deadline(plan: TradePlan, max_hold_minutes: float | None) -> datetime | None:
    return _max_hold_deadline_from_opened_at(plan.opened_at, max_hold_minutes)


def _as_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _exit_watch_ttl_limit(
    plan: TradePlan,
    *,
    max_hold_minutes: float | None,
    now: datetime,
) -> float:
    deadline = _max_hold_deadline(plan, max_hold_minutes)
    if deadline is None:
        return float(24 * 60)
    remaining_minutes = (deadline - now).total_seconds() / 60.0
    if remaining_minutes <= 0:
        raise InvalidExitPlanError("exit_watch_max_hold_elapsed")
    return remaining_minutes


def _cap_exit_watch_expiry_at_max_hold(
    watch: dict,
    *,
    max_hold_deadline: datetime | None,
) -> dict:
    """Keep an already persisted watch from outliving a shortened max hold."""
    if max_hold_deadline is None:
        return watch
    try:
        expires_at = datetime.fromisoformat(str(watch.get("expires_at")).replace("Z", "+00:00"))
    except ValueError:
        expires_at = None
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)
        if expires_at <= max_hold_deadline:
            return watch
    return {**watch, "expires_at": max_hold_deadline.isoformat()}


def apply_exit_update(
    plan: TradePlan,
    update: dict,
    *,
    bars: list | None = None,
    reference_price: float | None = None,
    now: datetime | None = None,
    trace_out: dict | None = None,
) -> TradePlan:
    """Patche les champs de sortie d'un TradePlan ouvert via un dict update normalisé.

    `update` est issu de `_compact_exit_plan` (clés internes : hard_stop?, take_profits?,
    trailing_stop?, profit_protection?, exit_watch?, max_hold_minutes?). Seuls les
    champs PRÉSENTS dans `update` sont patchés ; les autres champs du plan restent
    inchangés.

    Pour les TP en risk_multiple sans hard_stop dans l'update, utilise le
    `plan.hard_stop_price` existant comme référence de distance (injection transparente).

    Raises:
        InvalidExitPlanError: si la résolution du stop/TP échoue (bars manquants pour
            structural, stop_distance introuvable pour risk_multiple, etc.). Un
            amendement de ``exit_watch`` ou un nouveau ``max_hold_minutes`` non nul
            exige aussi l'horloge de décision ``now``.
    """
    if not update:
        return plan

    # Pour résoudre les TP en risk_multiple sans hard_stop dans l'update,
    # injecte le stop existant du plan comme référence de distance.
    resolve_input = dict(update)
    if "take_profits" in update and "hard_stop" not in update and plan.hard_stop_price is not None:
        resolve_input = {**update, "hard_stop": {"type": "price", "price": plan.hard_stop_price}}

    resolved, trace = resolve_exit_plan(
        resolve_input,
        entry_price=plan.entry_price,
        side=plan.side,
        reference_volatility=plan.reference_volatility,
        bars=bars,
        protective_reference_price=reference_price,
    )
    if trace_out is not None:
        trace_out.clear()
        trace_out.update(trace)

    if resolved is None:
        return plan

    patches: dict = {}

    if "hard_stop" in update:
        patches["hard_stop_price"] = _parse_price(resolved.get("hard_stop"))

    if "take_profits" in update:
        raw_tps = resolved.get("take_profits") or []
        quantity = plan.remaining_quantity
        remaining_fraction = 1.0
        new_tps: list[TakeProfit] = []
        for index, item in enumerate(raw_tps):
            if not isinstance(item, dict):
                continue
            fraction = float(item.get("fraction", remaining_fraction))
            fraction = max(0.0, min(fraction, remaining_fraction))
            remaining_fraction -= fraction
            new_tps.append(
                TakeProfit(
                    name=str(item.get("name") or f"tp{index + 1}"),
                    price=float(item["price"]),
                    fraction=fraction,
                    quantity=round(quantity * fraction, 8),
                    after_fill=str(item.get("after_fill") or ""),
                )
            )
        patches["take_profits"] = new_tps
        # Preserve filled state: keep names that exist in new TP list too.
        # (L'agent est responsable de choisir des noms cohérents.)
        new_tp_names = {tp.name for tp in new_tps}
        patches["filled_take_profits"] = [n for n in plan.filled_take_profits if n in new_tp_names]

    if "trailing_stop" in update:
        patches["trailing_stop"] = _trailing_from_dict(resolved.get("trailing_stop"))

    if "profit_protection" in update:
        patches["profit_protection"] = _profit_protection_from_raw(resolved.get("profit_protection"))

    effective_max_hold: float | None = None
    max_hold_deadline: datetime | None = None
    if "max_hold_minutes" in update or "exit_watch" in update:
        effective_max_hold = _effective_max_hold_minutes(plan, update, resolved)
        if "max_hold_minutes" in update or (
            "exit_watch" in update and resolved.get("exit_watch") is not None
        ):
            max_hold_deadline = _max_hold_deadline(plan, effective_max_hold)
        if "max_hold_minutes" in update and max_hold_deadline is not None:
            if now is None:
                raise InvalidExitPlanError("max_hold_now_required")
            if max_hold_deadline <= _as_utc(now):
                raise InvalidExitPlanError("max_hold_deadline_elapsed_use_strategy_close")

    if "max_hold_minutes" in update:
        patches["max_hold_minutes"] = effective_max_hold

    if "exit_watch" in update:
        raw_exit_watch = resolved.get("exit_watch")
        if raw_exit_watch is None:
            # La présence explicite de null est l'opération de clear du contrat
            # interne. Une forme non vide mais invalide est, elle, rejetée plus bas.
            patches["exit_watch"] = None
        else:
            if now is None:
                raise InvalidExitPlanError("exit_watch_now_required")
            amended_at = _as_utc(now)
            normalized_exit_watch = _normalize_exit_watch(
                raw_exit_watch,
                symbol=plan.symbol,
                opened_at=plan.opened_at,
                now=amended_at,
                max_ttl_minutes=_exit_watch_ttl_limit(
                    plan,
                    max_hold_minutes=effective_max_hold,
                    now=amended_at,
                ),
                preserve_last_triggered_at=False,
            )
            if normalized_exit_watch is None:
                # Un remplacement invalide ne doit jamais effacer la veille
                # active : l'amendement est atomiquement rejeté.
                raise InvalidExitPlanError("exit_watch_invalid")
            patches["exit_watch"] = normalized_exit_watch
    elif "max_hold_minutes" in update and isinstance(plan.exit_watch, dict):
        capped_exit_watch = _cap_exit_watch_expiry_at_max_hold(
            plan.exit_watch,
            max_hold_deadline=max_hold_deadline,
        )
        if capped_exit_watch != plan.exit_watch:
            patches["exit_watch"] = capped_exit_watch

    if not patches:
        return plan

    return plan.model_copy(update=patches)
