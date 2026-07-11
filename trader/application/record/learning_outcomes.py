"""Application policy for resolving decision-learning outcomes."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from trader.domain.learnings.scoring import (
    SIGNIFICANT_RETURN_BAND,
    classify_decision_quality,
)
from trader.domain.execution.fill_accounting import POSITION_EPSILON
from trader.domain.market_data import Bar

MIN_OUTCOME_AGE = timedelta(days=1)
UNKNOWN_OUTCOME_AGE = timedelta(days=3)
OPENING_INTENTS = frozenset({"OPEN_LONG", "OPEN_SHORT", "SCALE_IN", "FLIP"})
_FLAT_EPSILON = 1e-9

_WIN_CLASSES = {"gagnant", "bonne_prudence"}
_LOSS_CLASSES = {"perdant", "opportunite_manquee"}
_NEUTRAL_CLASSES = {"neutre", "justifie"}


class ModelPerformanceRows(Protocol):
    """Driven port exposing persisted model-performance rows."""

    def read_rows(self) -> Iterable[Mapping[str, object]]: ...


@dataclass
class _Lot:
    decision_id: str | None
    quantity: float
    price: float
    fx_rate: float
    entry_commission: float
    initial_quantity: float
    gross_usd: float = 0.0
    exit_commission: float = 0.0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_ts(raw: object) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(value)


def _non_negative(value: object, *, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else fallback


def realised_entry_outcomes(reader: ModelPerformanceRows) -> dict[str, float]:
    """Return net directional returns for fully closed FIFO entry lots."""
    rows = sorted(
        reader.read_rows(),
        key=lambda row: (str(row.get("symbol") or ""), str(row.get("ts") or "")),
    )
    lots_by_symbol: dict[str, deque[_Lot]] = {}
    resolved: dict[str, float] = {}

    for row in rows:
        symbol = str(row.get("symbol") or "")
        action = str(row.get("action") or "").upper()
        try:
            quantity = abs(float(row.get("quantity")))
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if (
            not symbol
            or action not in {"BUY", "SELL"}
            or not math.isfinite(quantity)
            or quantity <= 0.0
            or not math.isfinite(price)
            or price <= 0.0
        ):
            continue

        signed = quantity if action == "BUY" else -quantity
        fx_rate = _non_negative(row.get("fx_rate"), fallback=1.0)
        commission = _non_negative(row.get("commission"), fallback=0.0)
        decision_id = str(row.get("decision_id") or "") or None
        lots = lots_by_symbol.setdefault(symbol, deque())

        remaining = signed
        while lots and abs(remaining) > _FLAT_EPSILON and (lots[0].quantity > 0) != (remaining > 0):
            lot = lots[0]
            closed = min(abs(remaining), abs(lot.quantity))
            direction = 1.0 if lot.quantity > 0 else -1.0
            lot.gross_usd += (price - lot.price) * closed * direction * fx_rate
            lot.exit_commission += commission * (closed / abs(signed))
            lot.quantity -= direction * closed
            remaining += direction * closed
            if abs(lot.quantity) <= _FLAT_EPSILON:
                lots.popleft()
                if lot.decision_id:
                    deployed = lot.price * lot.initial_quantity * lot.fx_rate
                    net = lot.gross_usd - lot.entry_commission - lot.exit_commission
                    if deployed > 0.0:
                        resolved[lot.decision_id] = net / deployed

        if abs(remaining) > _FLAT_EPSILON:
            opening_quantity = abs(remaining)
            lots.append(
                _Lot(
                    decision_id=decision_id,
                    quantity=remaining,
                    price=price,
                    fx_rate=fx_rate,
                    entry_commission=commission * (opening_quantity / quantity),
                    initial_quantity=opening_quantity,
                )
            )

    return resolved


def realised_verdict(net_return: float) -> tuple[str, float]:
    if net_return > SIGNIFICANT_RETURN_BAND:
        return "WIN", 1.0
    if net_return < -SIGNIFICANT_RETURN_BAND:
        return "LOSS", -1.0
    return "NEUTRAL", 0.0


def requires_realised_trade(row: Mapping[str, object]) -> bool:
    return bool(row.get("executed")) and str(row.get("intent") or "") in OPENING_INTENTS


def _decision_quality_action(row: Mapping[str, object]) -> str | None:
    action = str(row.get("action") or "HOLD").upper()
    if action != "HOLD":
        return action

    snapshot = row.get("portfolio_snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    holdings = snapshot.get("holdings")
    if not isinstance(holdings, list):
        return None

    symbol = str(row.get("symbol") or "").strip().upper()
    quantity = 0.0
    for holding in holdings:
        if not isinstance(holding, Mapping) or holding.get("symbol") is None:
            return None
        if str(holding["symbol"]).strip().upper() != symbol:
            continue
        try:
            parsed_quantity = float(holding.get("quantity"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed_quantity):
            return None
        quantity += parsed_quantity

    if quantity > POSITION_EPSILON:
        return "BUY"
    if quantity < -POSITION_EPSILON:
        return "SELL"
    return "HOLD"


def _forward_return(bars: Sequence[Bar], ts: str, horizon: timedelta) -> float | None:
    ordered = sorted(bars, key=lambda bar: bar.ts)
    timestamps = [bar.ts for bar in ordered]
    i0 = bisect_right(timestamps, ts) - 1
    target = (datetime.fromisoformat(ts) + horizon).isoformat()
    ih = bisect_right(timestamps, target) - 1
    if i0 < 0 or ih <= i0:
        return None
    initial = ordered[i0].close
    if initial == 0.0:
        return None
    return ordered[ih].close / initial - 1.0


def _outcome(value: str, forward_value: float | None) -> dict[str, object]:
    if value in _WIN_CLASSES:
        return {"verdict": "WIN", "reward": 1.0, "forward_return": forward_value}
    if value in _LOSS_CLASSES:
        return {"verdict": "LOSS", "reward": -1.0, "forward_return": forward_value}
    if value in _NEUTRAL_CLASSES:
        return {"verdict": "NEUTRAL", "reward": 0.0, "forward_return": forward_value}
    return {"verdict": "UNKNOWN", "reward": None, "forward_return": None}


def score_outcome(
    row: Mapping[str, object],
    bars: Sequence[Bar],
    *,
    now: datetime,
) -> dict[str, object] | None:
    """Score one mature decision at 1d, then at the 4h fallback horizon."""
    ts = _parse_ts(row.get("ts") or row.get("cycle_ts"))
    symbol = str(row.get("symbol") or "")
    if ts is None or not symbol or _utc(now) - ts < MIN_OUTCOME_AGE:
        return None
    action = _decision_quality_action(row)
    if action is None:
        return None

    ts_iso = ts.isoformat()
    one_day = _forward_return(bars, ts_iso, timedelta(days=1))
    if one_day is not None:
        return _outcome(classify_decision_quality(action, one_day), one_day)
    four_hours = _forward_return(bars, ts_iso, timedelta(hours=4))
    if four_hours is not None:
        return _outcome(classify_decision_quality(action, four_hours), four_hours)
    if bars and _utc(now) - ts >= UNKNOWN_OUTCOME_AGE:
        return _outcome("non_evaluable", None)
    return None


def outcome_for_row(
    row: Mapping[str, object],
    *,
    bars: Sequence[Bar],
    now: datetime,
    realised_returns: Mapping[str, float],
) -> dict[str, object] | None:
    """Resolve an opening on realised P&L and every other decision on horizon quality."""
    decision_id = str(row.get("decision_id") or "")
    if uses_realised_outcome(row):
        net_return = realised_returns.get(decision_id)
        if net_return is None:
            return None
        verdict, reward = realised_verdict(net_return)
        return {
            "verdict": verdict,
            "reward": reward,
            "forward_return": net_return,
        }
    return score_outcome(row, bars, now=now)


def uses_realised_outcome(row: Mapping[str, object]) -> bool:
    """Whether a decision must await its fully realised trade result."""
    decision_id = str(row.get("decision_id") or "")
    return requires_realised_trade(row) and not decision_id.startswith("synth:")


__all__ = [
    "MIN_OUTCOME_AGE",
    "ModelPerformanceRows",
    "OPENING_INTENTS",
    "outcome_for_row",
    "realised_entry_outcomes",
    "realised_verdict",
    "requires_realised_trade",
    "score_outcome",
    "uses_realised_outcome",
]
