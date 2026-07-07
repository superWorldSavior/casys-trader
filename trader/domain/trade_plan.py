"""Pure persisted trade-plan contracts."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositionSide = Literal["LONG", "SHORT"]
MoveStopTo = Literal["breakeven", "none"]
TrailingStopTrailType = Literal["price", "percent", "volatility_multiple"]

POSITION_SIDES: tuple[PositionSide, ...] = ("LONG", "SHORT")
MOVE_STOP_TO_TYPES: tuple[MoveStopTo, ...] = ("breakeven", "none")
TRAILING_STOP_TRAIL_TYPES: tuple[TrailingStopTrailType, ...] = (
    "price",
    "percent",
    "volatility_multiple",
)


def _none_if_not_finite(value: object) -> object:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return value
    return parsed if math.isfinite(parsed) else None


class TakeProfit(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    price: float
    fraction: float
    quantity: float
    after_fill: str = ""


class TrailingStop(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled_after: str | None
    trail_type: TrailingStopTrailType
    trail_value: float
    trail_floored: bool = False


class ProfitProtection(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    arm_at_r: float = 0.5
    trigger_on_giveback_pct: float = 0.4
    close_fraction: float = 1.0 / 3.0
    move_stop_to: MoveStopTo = "breakeven"
    min_hold_minutes: float = 10.0
    lock_r: float | None = None
    triggered: bool = False


class TradePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    symbol: str
    side: PositionSide
    quantity: float
    remaining_quantity: float
    entry_price: float
    opened_at: str
    reference_volatility: float | None = None
    hard_stop_price: float | None = None
    take_profits: list[TakeProfit] = Field(default_factory=list)
    trailing_stop: TrailingStop | None = None
    max_hold_minutes: float | None = None
    high_watermark: float | None = None
    low_watermark: float | None = None
    filled_take_profits: list[str] = Field(default_factory=list)
    profit_protection: ProfitProtection | None = None
    exit_watch: dict | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None
    llm_confidence: float | None = None
    last_llm_review: dict | None = None
    entry_thesis: str | None = None
    entry_decision_id: str | None = None
    entry_context: dict | None = None

    @field_validator(
        "reference_volatility",
        "hard_stop_price",
        "high_watermark",
        "low_watermark",
        mode="before",
    )
    @classmethod
    def _coerce_non_finite_optional_float(cls, value: object) -> object:
        return _none_if_not_finite(value)

    @field_validator("trailing_stop", mode="before")
    @classmethod
    def _drop_non_finite_trailing_stop(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, TrailingStop):
            return None if not math.isfinite(value.trail_value) else value
        if not isinstance(value, dict):
            return value
        raw_trail_value = value.get("trail_value")
        if raw_trail_value is None:
            return None
        trail_value = _none_if_not_finite(raw_trail_value)
        if trail_value is None:
            return None
        return {**value, "trail_value": trail_value}
