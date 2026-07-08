"""Pure LLM exit-plan normalization and validation helpers."""

from __future__ import annotations

import math
from typing import Literal

from trader.domain.trade_plan import TRAILING_STOP_TRAIL_TYPES

StructuralHardStopAnchor = Literal["swing_low", "swing_high", "vwap"]
STRUCTURAL_HARD_STOP_ANCHORS: tuple[StructuralHardStopAnchor, ...] = (
    "swing_low",
    "swing_high",
    "vwap",
)


class InvalidExitPlanError(ValueError):
    """Raised when an agent-provided exit plan cannot be enforced safely."""


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


def _validate_optional_pct_bounds(raw: dict, field_name: str) -> None:
    min_pct = None
    max_pct = None
    if raw.get("min_pct") is not None:
        min_pct = _bounded_fraction(raw["min_pct"], f"{field_name}_min_pct")
    if raw.get("max_pct") is not None:
        max_pct = _bounded_fraction(raw["max_pct"], f"{field_name}_max_pct")
    if min_pct is not None and max_pct is not None and min_pct > max_pct:
        raise InvalidExitPlanError(f"{field_name}_min_pct_gt_max_pct")


def _positive_int(raw: object, field_name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise InvalidExitPlanError(f"{field_name}_must_be_positive")
    return raw


def _validate_structural_hard_stop(hard_stop: dict) -> None:
    if hard_stop.get("anchor") not in STRUCTURAL_HARD_STOP_ANCHORS:
        raise InvalidExitPlanError("hard_stop_anchor_unsupported")
    _positive_int(hard_stop.get("window"), "hard_stop_window")
    if hard_stop.get("buffer_pct") is not None and hard_stop.get("buffer_atr") is not None:
        raise InvalidExitPlanError("hard_stop_buffer_ambiguous")
    if hard_stop.get("buffer_pct") is not None:
        _positive_float(hard_stop["buffer_pct"], "hard_stop_buffer_pct")
    if hard_stop.get("buffer_atr") is not None:
        _positive_float(hard_stop["buffer_atr"], "hard_stop_buffer_atr")
    _validate_optional_pct_bounds(hard_stop, "hard_stop")


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
        if "arm_at_r" not in normalized_protection:
            for alias in ("arm_at_R", "arm_r", "after_r", "enabled_after_r", "activate_after_r"):
                if alias in normalized_protection:
                    normalized_protection["arm_at_r"] = normalized_protection[alias]
                    break
        if (
            "trigger_on_giveback_pct" not in normalized_protection
            and any(alias in normalized_protection for alias in ("giveback", "giveback_pct"))
        ):
            normalized_protection["trigger_on_giveback_pct"] = normalized_protection.get(
                "giveback",
                normalized_protection.get("giveback_pct"),
            )
        if "lock_r" not in normalized_protection:
            for alias in ("protect_r", "lock_in_r"):
                if alias in normalized_protection:
                    normalized_protection["lock_r"] = normalized_protection[alias]
                    break
        raw["profit_protection"] = normalized_protection

    return raw


def validate_exit_plan(
    raw_exit_plan: dict | None,
    *,
    reference_volatility: float | None = None,
    allow_unresolved: bool = False,
) -> None:
    """Validate that an exit plan can be normalized before any order is filled."""
    raw_exit_plan = normalize_exit_plan(raw_exit_plan)
    if raw_exit_plan is None:
        return

    hard_stop = raw_exit_plan.get("hard_stop")
    if hard_stop is not None:
        if isinstance(hard_stop, dict):
            hard_stop_type = str(hard_stop.get("type", "price"))
            if hard_stop_type == "price":
                if hard_stop.get("price") is None:
                    raise InvalidExitPlanError("hard_stop_price_required")
                _positive_float(hard_stop["price"], "hard_stop_price")
            elif allow_unresolved and hard_stop_type == "percent":
                _bounded_fraction(hard_stop.get("percent"), "hard_stop_percent")
                _validate_optional_pct_bounds(hard_stop, "hard_stop")
            elif allow_unresolved and hard_stop_type == "volatility_multiple":
                _positive_float(hard_stop.get("multiple"), "hard_stop_multiple")
                _validate_optional_pct_bounds(hard_stop, "hard_stop")
            elif allow_unresolved and hard_stop_type == "structural":
                _validate_structural_hard_stop(hard_stop)
            else:
                raise InvalidExitPlanError("hard_stop_type_unsupported")
        else:
            _positive_float(hard_stop, "hard_stop_price")

    take_profits = raw_exit_plan.get("take_profits", []) or []
    if not isinstance(take_profits, list):
        raise InvalidExitPlanError("take_profits_must_be_list")
    for index, item in enumerate(take_profits):
        prefix = f"take_profits_{index}"
        if not isinstance(item, dict):
            raise InvalidExitPlanError(f"{prefix}_must_be_object")
        if allow_unresolved and str(item.get("type", "price")) == "risk_multiple":
            _positive_float(item.get("r"), "take_profit_r")
            if item.get("fraction") is not None:
                _positive_float(item["fraction"], f"{prefix}_fraction")
            continue
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
            if protection.get("lock_r") is not None:
                lock_r = _non_negative_float(protection["lock_r"], "profit_protection_lock_r")
                arm_at_r = _positive_float(protection.get("arm_at_r", 0.5), "profit_protection_arm_at_r")
                if lock_r > arm_at_r:
                    raise InvalidExitPlanError("profit_protection_lock_r_gt_arm_at_r")
            move_stop_to = str(protection.get("move_stop_to", "breakeven"))
            if move_stop_to not in {"breakeven", "none"}:
                raise InvalidExitPlanError("profit_protection_move_stop_to_unsupported")
