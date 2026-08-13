"""Application policy for resolving decision-learning outcomes."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from trader.domain.decision_benchmark import decision_verdict
from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar

MIN_OUTCOME_AGE = timedelta(days=1)
UNKNOWN_OUTCOME_AGE = timedelta(days=3)
OPENING_INTENTS = frozenset({"OPEN_LONG", "OPEN_SHORT", "SCALE_IN", "FLIP"})
_FLAT_EPSILON = 1e-9


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
    if net_return >= SIGNIFICANT_RETURN_BAND:
        return "WIN", 1.0
    if net_return <= -SIGNIFICANT_RETURN_BAND:
        return "LOSS", -1.0
    return "NEUTRAL", 0.0


def requires_realised_trade(row: Mapping[str, object]) -> bool:
    return bool(row.get("executed")) and str(row.get("intent") or "") in OPENING_INTENTS


def forward_return(bars: Sequence[Bar], ts: str, horizon: timedelta) -> float | None:
    """Return from the as-of close to the first bar at/after ``horizon``.

    ISO strings cannot be sorted chronologically when their UTC offsets differ.
    Normalising both the decision and bar timestamps also keeps the bisect
    comparisons aware and deterministic around market-local offsets.
    """

    decision_ts = _parse_ts(ts)
    if decision_ts is None:
        return None
    dated_bars = [
        (bar_ts, bar)
        for bar in bars
        if (bar_ts := _parse_ts(bar.ts)) is not None
    ]
    dated_bars.sort(key=lambda item: item[0])
    timestamps = [bar_ts for bar_ts, _bar in dated_bars]
    i0 = bisect_right(timestamps, decision_ts) - 1
    target = decision_ts + horizon
    ih = bisect_left(timestamps, target)
    if i0 < 0 or ih >= len(dated_bars) or ih <= i0:
        return None
    initial = dated_bars[i0][1].close
    if initial == 0.0:
        return None
    return dated_bars[ih][1].close / initial - 1.0


def learning_outcome_for_return(
    row: Mapping[str, object],
    forward_value: float | None,
) -> dict[str, object]:
    """Map benchmark-v2 quality to the deliberately conservative learning reward."""

    value, _context = decision_verdict(row, forward_value, SIGNIFICANT_RETURN_BAND)
    if value == "good":
        # Un HOLD dans le bruit est correct pour l'audit, mais il ne prouve pas
        # qu'une règle d'abstention mérite une reward positive. Le garder neutre
        # évite de recréer un cliquet vers l'inaction.
        if (
            str(row.get("action") or "").upper() == "HOLD"
            and forward_value is not None
            and abs(forward_value) < SIGNIFICANT_RETURN_BAND
        ):
            return {"verdict": "NEUTRAL", "reward": 0.0, "forward_return": forward_value}
        return {"verdict": "WIN", "reward": 1.0, "forward_return": forward_value}
    if value in {"bad", "missed"}:
        return {"verdict": "LOSS", "reward": -1.0, "forward_return": forward_value}
    if value == "neutral":
        return {"verdict": "NEUTRAL", "reward": 0.0, "forward_return": forward_value}
    # Conserver le rendement observé permet de réévaluer plus tard une ligne
    # devenue directionnelle sans recharger l'historique prix.
    return {"verdict": "UNKNOWN", "reward": None, "forward_return": forward_value}


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

    ts_iso = ts.isoformat()
    one_day = forward_return(bars, ts_iso, timedelta(days=1))
    if one_day is not None:
        return learning_outcome_for_return(row, one_day)
    four_hours = forward_return(bars, ts_iso, timedelta(hours=4))
    if four_hours is not None:
        return learning_outcome_for_return(row, four_hours)
    if bars and _utc(now) - ts >= UNKNOWN_OUTCOME_AGE:
        return learning_outcome_for_return(row, None)
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
    "forward_return",
    "learning_outcome_for_return",
    "outcome_for_row",
    "realised_entry_outcomes",
    "realised_verdict",
    "requires_realised_trade",
    "score_outcome",
    "uses_realised_outcome",
]
