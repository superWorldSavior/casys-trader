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


def realised_entry_outcome_records(
    cycles: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Project fee-proven cycles onto entry decisions with explicit cycle identity.

    A decision mapped to two distinct ``position_cycle_id`` values is dropped
    instead of picking a winner.
    """
    resolved: dict[str, dict[str, object]] = {}
    ambiguous: set[str] = set()
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
        cycle_id = str(cycle.get("position_cycle_id") or "").strip() or None
        record = {
            "net_return": net_return,
            "source_cycle_id": cycle_id,
            "source_cycle_status": "available" if cycle_id else "unavailable",
        }
        for entry_id in entry_ids:
            normalized_id = str(entry_id or "").strip()
            if not normalized_id or normalized_id in ambiguous:
                continue
            existing = resolved.get(normalized_id)
            if existing is None:
                resolved[normalized_id] = dict(record)
                continue
            if (
                existing.get("source_cycle_id") != cycle_id
                or existing.get("net_return") != net_return
            ):
                ambiguous.add(normalized_id)
                resolved.pop(normalized_id, None)
    return resolved


def realised_entry_outcomes(
    cycles: Iterable[Mapping[str, object]],
) -> dict[str, float]:
    """Project canonical, fee-proven cycles onto their entry decisions.

    This application policy owns no persistence or reconstruction authority.
    Its caller must inject completed flat-to-flat cycles built from the broker
    ledger, including USD entry notional and explicit commission quality.
    """
    return {
        decision_id: float(record["net_return"])
        for decision_id, record in realised_entry_outcome_records(cycles).items()
        if isinstance(record.get("net_return"), (int, float))
    }


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


def _evaluation_provenance(
    outcome: Mapping[str, object],
    *,
    evaluation_basis: str,
    horizon_used: str | None,
    evaluated_at: datetime,
    source_cycle_id: str | None = None,
    source_cycle_status: str = "unavailable",
) -> dict[str, object]:
    return {
        **dict(outcome),
        "evaluation_basis": evaluation_basis,
        "horizon_used": horizon_used,
        "evaluated_at": _utc(evaluated_at).isoformat(),
        "source_cycle_id": source_cycle_id,
        "source_cycle_status": source_cycle_status,
    }


def _realised_record(raw: object) -> dict[str, object] | None:
    if isinstance(raw, Mapping):
        net_return = _finite_number(raw.get("net_return"))
        if net_return is None:
            return None
        cycle_id = str(raw.get("source_cycle_id") or "").strip() or None
        status = str(raw.get("source_cycle_status") or "").strip()
        if status not in {"available", "unavailable", "ambiguous"}:
            status = "available" if cycle_id else "unavailable"
        if status == "ambiguous":
            return None
        return {
            "net_return": net_return,
            "source_cycle_id": cycle_id,
            "source_cycle_status": status,
        }
    net_return = _finite_number(raw)
    if net_return is None:
        return None
    return {
        "net_return": net_return,
        "source_cycle_id": None,
        "source_cycle_status": "unavailable",
    }


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
        return _evaluation_provenance(
            learning_outcome_for_return(row, one_day),
            evaluation_basis="counterfactual",
            horizon_used="1d",
            evaluated_at=now,
        )
    four_hours = forward_return(bars, ts_iso, timedelta(hours=4))
    if four_hours is not None:
        return _evaluation_provenance(
            learning_outcome_for_return(row, four_hours),
            evaluation_basis="counterfactual",
            horizon_used="4h",
            evaluated_at=now,
        )
    if bars and _utc(now) - ts >= UNKNOWN_OUTCOME_AGE:
        return _evaluation_provenance(
            learning_outcome_for_return(row, None),
            evaluation_basis="counterfactual",
            horizon_used=None,
            evaluated_at=now,
        )
    return None


def outcome_for_row(
    row: Mapping[str, object],
    *,
    bars: Sequence[Bar],
    now: datetime,
    realised_returns: Mapping[str, object],
) -> dict[str, object] | None:
    """Resolve an opening on realised P&L and every other decision on horizon quality."""
    decision_id = str(row.get("decision_id") or "")
    if uses_realised_outcome(row):
        record = _realised_record(realised_returns.get(decision_id))
        if record is None:
            return None
        net_return = float(record["net_return"])
        verdict, reward = realised_verdict(net_return)
        return _evaluation_provenance(
            {
                "verdict": verdict,
                "reward": reward,
                "forward_return": net_return,
            },
            evaluation_basis="realized",
            horizon_used=None,
            evaluated_at=now,
            source_cycle_id=str(record["source_cycle_id"]) if record.get("source_cycle_id") else None,
            source_cycle_status=str(record["source_cycle_status"]),
        )
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
    "realised_entry_outcome_records",
    "realised_entry_outcomes",
    "realised_verdict",
    "requires_realised_trade",
    "score_outcome",
    "uses_realised_outcome",
]
