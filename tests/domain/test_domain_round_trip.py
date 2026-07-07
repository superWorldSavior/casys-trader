from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from trader.domain.trade_plan import TradePlan
from trader.execution.contracts import Commission, Fill, Order, Position


def _domain_dump(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"unsupported domain object: {type(value)!r}")


def _compact_json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


TRADE_PLAN_JSON = {
    "id": "ROUNDTRIP-2026-07-06T175835.961967Z0000",
    "symbol": "AWK",
    "side": "LONG",
    "quantity": 35.0,
    "remaining_quantity": 17.5,
    "entry_price": 133.1199951171875,
    "opened_at": "2026-07-06T17:58:35.961967+00:00",
    "reference_volatility": 2.253322157348633,
    "hard_stop_price": 131.60064001464843,
    "take_profits": [
        {
            "name": "tp1",
            "price": 135.19934777832032,
            "fraction": 0.5,
            "quantity": 17.5,
            "after_fill": "move_stop_to_breakeven",
        },
        {
            "name": "tp2",
            "price": 136.58558288574216,
            "fraction": 0.5,
            "quantity": 17.5,
            "after_fill": "close",
        },
    ],
    "trailing_stop": {
        "enabled_after": "tp1",
        "trail_type": "volatility_multiple",
        "trail_value": 1.25,
        "trail_floored": True,
    },
    "max_hold_minutes": 240.0,
    "high_watermark": 133.88999938964844,
    "low_watermark": 132.91000366210938,
    "filled_take_profits": ["tp1"],
    "profit_protection": {
        "enabled": True,
        "arm_at_r": 1.0,
        "trigger_on_giveback_pct": 0.4,
        "close_fraction": 0.3333333333333333,
        "move_stop_to": "breakeven",
        "min_hold_minutes": 240.0,
        "lock_r": 0.1,
        "triggered": False,
    },
    "exit_watch": {
        "id": "AWK-exit-watch",
        "symbol": "AWK",
        "created_at": "2026-07-06T17:58:35.961967+00:00",
        "expires_at": "2026-07-06T21:58:35.961967+00:00",
        "logic": "any",
        "on_trigger": "WAKE",
        "source": "exit_watch",
        "cooldown_minutes": 15.0,
        "last_triggered_at": "2026-07-06T19:58:35.961967+00:00",
        "conditions": [
            {"indicator": "trend_slope", "op": "<", "value": 0.0, "timeframe": "15m"},
            {"indicator": "rsi", "op": ">", "value": 72.0, "timeframe": "1h"},
        ],
    },
    "llm_provider": "acpx",
    "llm_model": "gpt-5.5",
    "llm_fallback_reason": "provider_timeout",
    "llm_confidence": 0.73,
    "last_llm_review": {
        "ts": "2026-07-06T19:59:56.347307+00:00",
        "verdict": "intact",
        "action": "HOLD",
        "intent": "HOLD",
        "llm_provider": "acpx",
        "llm_model": "gpt-5.5",
        "confidence": 0.72,
        "notes": {"risk": "contained", "freshness": "ok"},
    },
    "entry_thesis": "Round-trip fixture with every TradePlan field populated.",
    "entry_decision_id": "decision-2026-07-06T175835.961967Z0000",
    "entry_context": {
        "price": 133.1199951171875,
        "runtime_interval": "15m",
        "data_age_m": 14,
        "session_open": True,
        "daily_as_of": "2026-07-06T00:00:00-04:00",
        "features": {"z": 0.42, "rs": 1.12},
    },
}


FILL_JSON = {
    "symbol": "AWK",
    "side": "BUY",
    "quantity": 17.5,
    "price": 133.1199951171875,
    "ts": "2026-07-06T17:58:35.961967+00:00",
    "commission": 1.23,
    "commission_currency": "USD",
    "commission_model": "ibkr",
    "fx_rate": 1.0,
}

ORDER_JSON = {
    "symbol": "AWK",
    "side": "BUY",
    "quantity": 17.5,
    "rationale": "OPEN_LONG intent after validated utility breakout",
}

COMMISSION_JSON = {
    "amount": 1.23,
    "currency": "USD",
    "model": "ibkr_us_stock_tiered",
}

POSITION_JSON = {
    "symbol": "AWK",
    "quantity": 35.0,
    "avg_price": 133.1199951171875,
}


def test_trade_plan_round_trip_serializes_byte_exact() -> None:
    plan = TradePlan.model_validate(TRADE_PLAN_JSON)

    serialized = _domain_dump(plan)

    assert serialized == TRADE_PLAN_JSON
    assert _compact_json_bytes(serialized) == _compact_json_bytes(TRADE_PLAN_JSON)


def test_fill_round_trip_serializes_byte_exact() -> None:
    fill = Fill(**FILL_JSON)

    serialized = _domain_dump(fill)

    assert serialized == FILL_JSON
    assert _compact_json_bytes(serialized) == _compact_json_bytes(FILL_JSON)


def test_order_round_trip_serializes_byte_exact() -> None:
    order = Order(**ORDER_JSON)

    serialized = _domain_dump(order)

    assert serialized == ORDER_JSON
    assert _compact_json_bytes(serialized) == _compact_json_bytes(ORDER_JSON)


def test_commission_round_trip_serializes_byte_exact() -> None:
    commission = Commission(**COMMISSION_JSON)

    serialized = _domain_dump(commission)

    assert serialized == COMMISSION_JSON
    assert _compact_json_bytes(serialized) == _compact_json_bytes(COMMISSION_JSON)


def test_position_round_trip_serializes_byte_exact() -> None:
    position = Position(**POSITION_JSON)

    serialized = _domain_dump(position)

    assert serialized == POSITION_JSON
    assert _compact_json_bytes(serialized) == _compact_json_bytes(POSITION_JSON)
