"""Application policy for resolving decision-learning outcomes."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone

from trader.domain.decision_benchmark import decision_verdict
from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar

MIN_OUTCOME_AGE = timedelta(days=1)
UNKNOWN_OUTCOME_AGE = timedelta(days=3)
OPENING_INTENTS = frozenset({"OPEN_LONG", "OPEN_SHORT", "SCALE_IN", "FLIP"})


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


def _finite_number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def realised_entry_outcomes(
    cycles: Iterable[Mapping[str, object]],
) -> dict[str, float]:
    """Project canonical, fee-proven cycles onto their entry decisions.

    This application policy owns no persistence or reconstruction authority.
    Its caller must inject completed flat-to-flat cycles built from the broker
    ledger, including USD entry notional and explicit commission quality.
    """
    resolved: dict[str, float] = {}
    for cycle in cycles:
        quality = cycle.get("commission_quality")
        if not isinstance(quality, Mapping) or quality.get("status") != "available":
            continue
        deployed = _finite_number(cycle.get("entry_notional_usd"))
        net_pnl = _finite_number(cycle.get("pnl"))
        if deployed is None or deployed <= 0.0 or net_pnl is None:
            continue

        raw_entry_ids = cycle.get("entry_decision_ids")
        entry_ids = raw_entry_ids if isinstance(raw_entry_ids, list) else []
        net_return = net_pnl / deployed
        if not math.isfinite(net_return):
            continue
        for entry_id in entry_ids:
            normalized_id = str(entry_id or "").strip()
            if normalized_id:
                resolved[normalized_id] = net_return
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
