"""Persisted trade plans defined by the agent and enforced by the daemon."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .indicator_watch import normalize_indicator_watch

PositionSide = Literal["LONG", "SHORT"]
MoveStopTo = Literal["breakeven", "none"]
TrailingStopTrailType = Literal["price", "percent", "volatility_multiple"]

TRAILING_STOP_TRAIL_TYPES: tuple[TrailingStopTrailType, ...] = (
    "price",
    "percent",
    "volatility_multiple",
)


class InvalidExitPlanError(ValueError):
    """Raised when an agent-provided exit plan cannot be enforced safely."""


@dataclass(frozen=True)
class TakeProfit:
    name: str
    price: float
    fraction: float
    quantity: float
    after_fill: str = ""


@dataclass(frozen=True)
class TrailingStop:
    enabled_after: str | None
    trail_type: TrailingStopTrailType
    trail_value: float
    trail_floored: bool = False


@dataclass(frozen=True)
class ProfitProtection:
    enabled: bool = True
    arm_at_r: float = 0.5
    trigger_on_giveback_pct: float = 0.4
    close_fraction: float = 1.0 / 3.0
    move_stop_to: MoveStopTo = "breakeven"
    min_hold_minutes: float = 10.0
    triggered: bool = False


@dataclass(frozen=True)
class TradePlan:
    id: str
    symbol: str
    side: PositionSide
    quantity: float
    remaining_quantity: float
    entry_price: float
    opened_at: str
    reference_volatility: float | None = None
    hard_stop_price: float | None = None
    take_profits: list[TakeProfit] = field(default_factory=list)
    trailing_stop: TrailingStop | None = None
    max_hold_minutes: float | None = None
    high_watermark: float | None = None
    low_watermark: float | None = None
    filled_take_profits: list[str] = field(default_factory=list)
    profit_protection: ProfitProtection | None = None
    exit_watch: dict | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None
    llm_confidence: float | None = None


def _parse_price(raw: object) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return float(raw)
    if raw.get("type", "price") != "price":
        return None
    price = raw.get("price")
    return None if price is None else float(price)


def _positive_float(raw: object, field_name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidExitPlanError(f"{field_name}_must_be_number") from exc
    if not math.isfinite(value):
        raise InvalidExitPlanError(f"{field_name}_must_be_finite")
    if value <= 0:
        raise InvalidExitPlanError(f"{field_name}_must_be_positive")
    return value


def _bounded_fraction(raw: object, field_name: str) -> float:
    value = _positive_float(raw, field_name)
    if value > 1:
        raise InvalidExitPlanError(f"{field_name}_must_be_lte_1")
    return value


def _non_negative_float(raw: object, field_name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidExitPlanError(f"{field_name}_must_be_number") from exc
    if value < 0:
        raise InvalidExitPlanError(f"{field_name}_must_be_non_negative")
    return value


def _bounded_float(value: object, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except Exception:
        parsed = default
    return min(max(parsed, minimum), maximum)


def normalize_exit_plan(raw_exit_plan: dict | None) -> dict | None:
    """Normalize common compact LLM exit-plan shapes into the enforced schema."""
    if raw_exit_plan is None:
        return None
    if not isinstance(raw_exit_plan, dict):
        raise InvalidExitPlanError("exit_plan_must_be_object")

    raw = dict(raw_exit_plan)
    if "hard_stop" not in raw:
        for alias in ("stop_loss", "stop", "sl"):
            if alias in raw:
                raw["hard_stop"] = raw[alias]
                break

    take_profits = raw.get("take_profits", []) or []
    if isinstance(take_profits, list):
        normalized_tps: list[dict] = []
        for index, item in enumerate(take_profits):
            if isinstance(item, dict):
                normalized = dict(item)
                if "price" not in normalized:
                    for alias in ("target", "tp", "take_profit", "level"):
                        if alias in normalized:
                            normalized["price"] = normalized[alias]
                            break
                normalized.setdefault("name", f"tp{index + 1}")
            else:
                normalized = {"name": f"tp{index + 1}", "price": item}
            normalized_tps.append(normalized)

        missing_fraction = [item for item in normalized_tps if item.get("fraction") is None]
        if normalized_tps and len(missing_fraction) == len(normalized_tps):
            equal_fraction = 1.0 / len(normalized_tps)
            for item in normalized_tps:
                item["fraction"] = equal_fraction
        elif missing_fraction:
            used = 0.0
            for item in normalized_tps:
                if item.get("fraction") is not None:
                    try:
                        used += float(item["fraction"])
                    except (TypeError, ValueError):
                        pass
            fallback_fraction = max(0.0, 1.0 - used) / len(missing_fraction)
            for item in missing_fraction:
                item["fraction"] = fallback_fraction
        raw["take_profits"] = normalized_tps

    trailing = raw.get("trailing_stop")
    if trailing in ("", False):
        raw["trailing_stop"] = None
    elif trailing is not None and not isinstance(trailing, dict):
        raw["trailing_stop"] = {"trail_type": "price", "trail_value": trailing}
    elif isinstance(trailing, dict):
        normalized_trailing = dict(trailing)
        if normalized_trailing.get("trail_value") is None:
            for alias in ("value", "distance", "amount", "trail"):
                if normalized_trailing.get(alias) is not None:
                    normalized_trailing["trail_value"] = normalized_trailing[alias]
                    break
        if normalized_trailing.get("trail_value") is None:
            raw["trailing_stop"] = None
        else:
            normalized_trailing.setdefault("trail_type", "price")
            raw["trailing_stop"] = normalized_trailing

    protection = raw.get("profit_protection")
    if protection in (False, "", None):
        if "profit_protection" in raw:
            raw["profit_protection"] = None
    elif protection is True:
        raw["profit_protection"] = {}
    elif isinstance(protection, dict):
        normalized_protection = dict(protection)
        if "arm_at_r" not in normalized_protection and "arm_at_R" in normalized_protection:
            normalized_protection["arm_at_r"] = normalized_protection["arm_at_R"]
        if (
            "trigger_on_giveback_pct" not in normalized_protection
            and "giveback_pct" in normalized_protection
        ):
            normalized_protection["trigger_on_giveback_pct"] = normalized_protection["giveback_pct"]
        raw["profit_protection"] = normalized_protection

    return raw


def validate_exit_plan(
    raw_exit_plan: dict | None,
    *,
    reference_volatility: float | None = None,
) -> None:
    """Validate that an exit plan can be normalized before any order is filled."""
    raw_exit_plan = normalize_exit_plan(raw_exit_plan)
    if raw_exit_plan is None:
        return

    hard_stop = raw_exit_plan.get("hard_stop")
    if hard_stop is not None:
        if isinstance(hard_stop, dict):
            if hard_stop.get("type", "price") != "price":
                raise InvalidExitPlanError("hard_stop_type_unsupported")
            if hard_stop.get("price") is None:
                raise InvalidExitPlanError("hard_stop_price_required")
            _positive_float(hard_stop["price"], "hard_stop_price")
        else:
            _positive_float(hard_stop, "hard_stop_price")

    take_profits = raw_exit_plan.get("take_profits", []) or []
    if not isinstance(take_profits, list):
        raise InvalidExitPlanError("take_profits_must_be_list")
    for index, item in enumerate(take_profits):
        prefix = f"take_profits_{index}"
        if not isinstance(item, dict):
            raise InvalidExitPlanError(f"{prefix}_must_be_object")
        if item.get("price") is None:
            raise InvalidExitPlanError(f"{prefix}_price_required")
        _positive_float(item["price"], f"{prefix}_price")
        if item.get("fraction") is not None:
            _positive_float(item["fraction"], f"{prefix}_fraction")

    trailing = raw_exit_plan.get("trailing_stop")
    if trailing is not None:
        if not isinstance(trailing, dict):
            raise InvalidExitPlanError("trailing_stop_must_be_object")
        trail_type = str(trailing.get("trail_type", "price"))
        if trail_type not in TRAILING_STOP_TRAIL_TYPES:
            raise InvalidExitPlanError("trailing_stop_type_unsupported")
        if trailing.get("trail_value") is None:
            raise InvalidExitPlanError("trailing_stop_trail_value_required")
        _positive_float(trailing["trail_value"], "trailing_stop_trail_value")
        if trail_type == "volatility_multiple":
            if reference_volatility is None:
                raise InvalidExitPlanError("trailing_volatility_unavailable")
            _positive_float(reference_volatility, "reference_volatility")

    if raw_exit_plan.get("max_hold_minutes") is not None:
        _positive_float(raw_exit_plan["max_hold_minutes"], "max_hold_minutes")

    protection = raw_exit_plan.get("profit_protection")
    if protection is not None:
        if not isinstance(protection, dict):
            raise InvalidExitPlanError("profit_protection_must_be_object")
        if protection.get("enabled", True) is not False:
            if protection.get("arm_at_r") is not None:
                _positive_float(protection["arm_at_r"], "profit_protection_arm_at_r")
            if protection.get("trigger_on_giveback_pct") is not None:
                _bounded_fraction(
                    protection["trigger_on_giveback_pct"],
                    "profit_protection_trigger_on_giveback_pct",
                )
            if protection.get("close_fraction") is not None:
                _bounded_fraction(protection["close_fraction"], "profit_protection_close_fraction")
            if protection.get("min_hold_minutes") is not None:
                _non_negative_float(
                    protection["min_hold_minutes"],
                    "profit_protection_min_hold_minutes",
                )
            move_stop_to = str(protection.get("move_stop_to", "breakeven"))
            if move_stop_to not in {"breakeven", "none"}:
                raise InvalidExitPlanError("profit_protection_move_stop_to_unsupported")


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
        max_ttl_minutes=float(raw.get("max_hold_minutes") or 24 * 60),
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
        max_hold_minutes=(
            None
            if raw.get("max_hold_minutes") is None
            else float(raw["max_hold_minutes"])
        ),
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


def _take_profit_from_dict(raw: dict) -> TakeProfit:
    return TakeProfit(
        name=str(raw["name"]),
        price=float(raw["price"]),
        fraction=float(raw["fraction"]),
        quantity=float(raw["quantity"]),
        after_fill=str(raw.get("after_fill", "")),
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
        triggered=bool(raw.get("triggered", False)),
    )


def _normalize_exit_watch(
    raw: object,
    *,
    symbol: str,
    opened_at: str,
    max_ttl_minutes: float,
) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        created_at = datetime.fromisoformat(opened_at)
    except ValueError:
        created_at = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
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
    if raw.get("last_triggered_at"):
        watch["last_triggered_at"] = str(raw["last_triggered_at"])
    return watch


def trade_plan_from_dict(raw: dict) -> TradePlan:
    trailing_stop = _trailing_from_dict(raw.get("trailing_stop"))
    profit_protection = _profit_protection_from_raw(raw.get("profit_protection"))
    raw_reference_volatility = raw.get("reference_volatility")
    reference_volatility = (
        None if raw_reference_volatility is None else float(raw_reference_volatility)
    )
    if reference_volatility is not None and not math.isfinite(reference_volatility):
        reference_volatility = None
    return TradePlan(
        id=str(raw["id"]),
        symbol=str(raw["symbol"]),
        side=raw["side"],
        quantity=float(raw["quantity"]),
        remaining_quantity=float(raw["remaining_quantity"]),
        entry_price=float(raw["entry_price"]),
        opened_at=str(raw["opened_at"]),
        reference_volatility=reference_volatility,
        hard_stop_price=None if raw.get("hard_stop_price") is None else float(raw["hard_stop_price"]),
        take_profits=[_take_profit_from_dict(tp) for tp in raw.get("take_profits", [])],
        trailing_stop=trailing_stop,
        max_hold_minutes=None if raw.get("max_hold_minutes") is None else float(raw["max_hold_minutes"]),
        high_watermark=None if raw.get("high_watermark") is None else float(raw["high_watermark"]),
        low_watermark=None if raw.get("low_watermark") is None else float(raw["low_watermark"]),
        filled_take_profits=[str(name) for name in raw.get("filled_take_profits", [])],
        profit_protection=profit_protection,
        exit_watch=(dict(raw["exit_watch"]) if isinstance(raw.get("exit_watch"), dict) else None),
        llm_provider=None if raw.get("llm_provider") is None else str(raw["llm_provider"]),
        llm_model=None if raw.get("llm_model") is None else str(raw["llm_model"]),
        llm_fallback_reason=(
            None
            if raw.get("llm_fallback_reason") is None
            else str(raw["llm_fallback_reason"])
        ),
        llm_confidence=None if raw.get("llm_confidence") is None else float(raw["llm_confidence"]),
    )


class TradePlanStore:
    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"plans": []}
        return json.loads(self.state_path.read_text())

    def _save(self, data: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def clear(self) -> None:
        self._save({"plans": []})

    def open_plans(self) -> list[TradePlan]:
        return [trade_plan_from_dict(raw) for raw in self._load().get("plans", [])]

    def upsert(self, plan: TradePlan) -> None:
        data = self._load()
        plans = [raw for raw in data.get("plans", []) if raw.get("id") != plan.id]
        plans.append(asdict(plan))
        self._save({"plans": plans})

    def close(self, plan_id: str) -> None:
        data = self._load()
        plans = [raw for raw in data.get("plans", []) if raw.get("id") != plan_id]
        self._save({"plans": plans})

    def close_symbol(self, symbol: str) -> None:
        data = self._load()
        plans = [raw for raw in data.get("plans", []) if raw.get("symbol") != symbol]
        self._save({"plans": plans})

    def sync_symbol_quantity(self, symbol: str, remaining_quantity: float) -> None:
        if remaining_quantity <= 0:
            self.close_symbol(symbol)
            return

        data = self._load()
        raw_plans = list(data.get("plans", []))
        symbol_plans = [
            trade_plan_from_dict(raw)
            for raw in raw_plans
            if raw.get("symbol") == symbol
        ]
        total_remaining = sum(plan.remaining_quantity for plan in symbol_plans)
        if total_remaining <= 0:
            self.close_symbol(symbol)
            return

        ratio = remaining_quantity / total_remaining
        updated: list[dict] = []
        for raw in raw_plans:
            if raw.get("symbol") != symbol:
                updated.append(raw)
                continue

            plan = trade_plan_from_dict(raw)
            new_remaining = round(plan.remaining_quantity * ratio, 8)
            if new_remaining <= 0:
                continue
            take_profits = [
                tp
                if tp.name in plan.filled_take_profits
                else replace(tp, quantity=round(tp.quantity * ratio, 8))
                for tp in plan.take_profits
            ]
            updated.append(asdict(replace(plan, remaining_quantity=new_remaining, take_profits=take_profits)))

        self._save({"plans": updated})
