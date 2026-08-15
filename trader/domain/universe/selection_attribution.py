"""Pure attribution of universe selections against symbol movement.

Judges the selection, not the trader: a long-only pick that subsequently
moved up is a good selection even if no trade was taken.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from trader.domain.learnings.scoring import (
    SIGNIFICANT_RETURN_BAND,
    classify_decision_quality,
    compute_outcome_scores,
)
from trader.domain.market_data import Bar

DEFAULT_FORWARD_SESSIONS = 5
DEFAULT_SHRINKAGE_K = 5.0

__all__ = [
    "DEFAULT_FORWARD_SESSIONS",
    "DEFAULT_SHRINKAGE_K",
    "classify_selection_quality",
    "directional_action",
    "forward_return_over_sessions",
    "score_selection_outcomes",
    "to_flair_verdict",
]


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


def directional_action(allowed_sides: Sequence[str] | None) -> str | None:
    """Translate a directional mandate into the scoring action, or ``None``."""
    sides = {str(side).strip().lower() for side in (allowed_sides or ()) if str(side).strip()}
    sides &= {"long", "short"}
    if sides == {"long"}:
        return "BUY"
    if sides == {"short"}:
        return "SELL"
    return None


def forward_return_over_sessions(
    bars: Sequence[Bar],
    as_of: str,
    horizon_sessions: int = DEFAULT_FORWARD_SESSIONS,
) -> float | None:
    """Close-to-close return from the as-of session to ``horizon_sessions`` later.

    Start bar is the last bar at or before ``as_of``. If the series begins after
    activation, the first bar at or after ``as_of`` is used instead. The end bar
    is exactly ``horizon_sessions`` sessions after that start.
    """
    if horizon_sessions <= 0:
        return None
    as_of_ts = _parse_ts(as_of)
    if as_of_ts is None:
        return None
    dated = [(bar_ts, bar) for bar in bars if (bar_ts := _parse_ts(bar.ts)) is not None]
    dated.sort(key=lambda item: item[0])
    if not dated:
        return None
    timestamps = [item[0] for item in dated]
    start = bisect_right(timestamps, as_of_ts) - 1
    if start < 0:
        start = bisect_left(timestamps, as_of_ts)
        if start >= len(dated):
            return None
    end = start + horizon_sessions
    if end >= len(dated):
        return None
    initial = dated[start][1].close
    if initial == 0.0:
        return None
    return dated[end][1].close / initial - 1.0


def classify_selection_quality(
    allowed_sides: Sequence[str] | None,
    forward_return: float | None,
    *,
    band: float = SIGNIFICANT_RETURN_BAND,
) -> str:
    """Verdict of one selection. Non-directional sides are ``non_evaluable``."""
    action = directional_action(allowed_sides)
    if action is None:
        return "non_evaluable"
    return classify_decision_quality(action, forward_return, band)


def to_flair_verdict(verdict: str) -> str:
    """Map a French quality verdict onto the WIN/LOSS vocabulary FLAIR expects."""
    if verdict == "gagnant":
        return "WIN"
    if verdict == "perdant":
        return "LOSS"
    return verdict


def score_selection_outcomes(
    rows: Iterable[Mapping[str, Any]],
    *,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
) -> dict:
    """Run persistence-agnostic FLAIR on already-judged selections."""
    mapped = [
        {
            "id": int(row["id"]),
            "symbol": row.get("symbol") or "",
            "family": row.get("family") or "",
            "verdict": to_flair_verdict(str(row.get("verdict") or "")),
        }
        for row in rows
    ]
    return compute_outcome_scores(mapped, shrinkage_k=shrinkage_k)
