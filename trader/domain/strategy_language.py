"""Canonical Pine-like strategy language value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StrategyPrimitive = Literal[
    "position_order",
    "exit_rule",
    "set_next_wake",
    "propose_indicator_watch",
    "cancel_watch",
    "record_learning",
]
StrategyDialect = Literal["pine_json", "system"]

__all__ = [
    "CompiledStrategyCall",
    "StrategyDialect",
    "StrategyPrimitive",
    "compile_strategy_call",
    "normalize_trade_thesis",
]

_MAX_THESIS_FIELD_CHARS = 200
_THESIS_VALID_HORIZONS = frozenset({"intraday", "swing", "position"})


@dataclass(frozen=True)
class CompiledStrategyCall:
    public_tool: str
    primitive: StrategyPrimitive
    args: dict
    dialect: StrategyDialect
    position_aware: bool = False


def normalize_trade_thesis(value: object) -> dict | None:
    """Canonicalize an optional setup/horizon/invalidation thesis tag."""

    if not isinstance(value, dict):
        return None
    setup = value.get("setup")
    horizon = value.get("horizon")
    invalidation = value.get("invalidation")
    if not isinstance(setup, str) or not isinstance(horizon, str):
        return None
    if not isinstance(invalidation, str):
        return None
    setup = setup.strip()[:_MAX_THESIS_FIELD_CHARS]
    invalidation = invalidation.strip()[:_MAX_THESIS_FIELD_CHARS]
    horizon_norm = horizon.strip().lower()
    if (
        not setup
        or not invalidation
        or horizon_norm not in _THESIS_VALID_HORIZONS
    ):
        return None
    return {
        "setup": setup,
        "horizon": horizon_norm,
        "invalidation": invalidation,
    }


def _pine_qty_percent_fraction(args: dict) -> float | None:
    raw = args.get("qty_percent")
    if raw is None:
        return None
    fraction = float(raw) / 100.0
    if not (0.0 < fraction <= 1.0):
        raise ValueError("qty_percent_out_of_range")
    return fraction


def _pine_exit_args(args: dict) -> dict:
    if not isinstance(args, dict):
        raise ValueError("strategy_exit_args_must_be_object")
    out: dict = {}
    fraction = _pine_qty_percent_fraction(args)
    has_limit = args.get("limit") is not None
    has_stop = args.get("stop") is not None
    has_partial_qty = fraction is not None and fraction < 1.0
    if has_limit and has_stop and fraction is not None and fraction < 1.0:
        raise ValueError("partial_bracket_exit_not_supported")
    if has_partial_qty and has_stop:
        raise ValueError("partial_stop_exit_not_supported")
    if has_partial_qty and not has_limit:
        raise ValueError("qty_percent_requires_limit_only")
    if has_stop:
        out["stop"] = args["stop"]
    if args.get("hard_stop") is not None:
        out["hard_stop"] = args["hard_stop"]
    if has_limit:
        tp: dict = {"type": "price", "price": args["limit"]}
        tp["fraction"] = 1.0 if fraction is None else fraction
        if args.get("id") is not None:
            tp["name"] = args["id"]
        out["tp"] = [tp]
    elif args.get("tp") is not None:
        out["tp"] = args["tp"]
    elif args.get("take_profits") is not None:
        out["take_profits"] = args["take_profits"]
    if args.get("trail") is not None:
        out["trail"] = args["trail"]
    elif args.get("trail_offset") is not None:
        out["trail"] = {
            "type": args.get("trail_type") or args.get("offset_type") or "price",
            "value": args["trail_offset"],
        }
    if args.get("protect") is not None:
        out["protect"] = args["protect"]
    if args.get("exit_watch") is not None:
        out["exit_watch"] = args["exit_watch"]
    if args.get("max_hold_minutes") is not None:
        out["max_hold_minutes"] = args["max_hold_minutes"]
    return out


def _strategy_entry_args(args: dict) -> dict:
    raw_direction = str(args.get("direction") or args.get("side") or "").lower()
    direction = raw_direction.replace("strategy.", "")
    if direction in {"long", "buy"}:
        intent = "OPEN_LONG"
    elif direction in {"short", "sell"}:
        intent = "OPEN_SHORT"
    else:
        raise ValueError("strategy_entry_direction_required")
    out: dict = {"intent": intent}
    for key in (
        "qty",
        "quantity",
        "risk_pct",
        "thesis",
        "evaluation_id",
        "trade_evaluation_id",
    ):
        if args.get(key) is not None:
            out[key] = args[key]
    if args.get("exit") is not None:
        out["exit"] = _pine_exit_args(dict(args["exit"]))
    return out


def _strategy_close_args(args: dict) -> dict:
    fraction = _pine_qty_percent_fraction(args)
    if fraction is not None and fraction < 1.0:
        return {"intent": "REDUCE", "fraction": fraction}
    if args.get("qty") is not None:
        return {"intent": "REDUCE", "qty": args["qty"]}
    if args.get("quantity") is not None:
        return {"intent": "REDUCE", "quantity": args["quantity"]}
    return {"intent": "CLOSE"}


def compile_strategy_call(tool: str, args: dict) -> CompiledStrategyCall:
    """Compile one public strategy call into the daemon's stable primitives."""
    if tool == "strategy_entry":
        return CompiledStrategyCall(
            public_tool=tool,
            primitive="position_order",
            args=_strategy_entry_args(args),
            dialect="pine_json",
            position_aware=True,
        )
    if tool == "strategy_exit":
        return CompiledStrategyCall(
            public_tool=tool,
            primitive="exit_rule",
            args=_pine_exit_args(args),
            dialect="pine_json",
        )
    if tool == "strategy_close":
        return CompiledStrategyCall(
            public_tool=tool,
            primitive="position_order",
            args=_strategy_close_args(args),
            dialect="pine_json",
            position_aware=True,
        )

    if tool in {"set_next_wake", "propose_indicator_watch", "cancel_watch", "record_learning"}:
        return CompiledStrategyCall(tool, tool, dict(args), "system")

    raise ValueError(f"unknown_action_tool:{tool}")
