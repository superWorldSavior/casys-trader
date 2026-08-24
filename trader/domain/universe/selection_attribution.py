"""Pure attribution of universe selections against symbol movement.

Judges the selection, not the trader: a long-only pick that subsequently
moved up is a good selection even if no trade was taken.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from statistics import median
from typing import Any

from trader.domain.learnings.scoring import (
    SIGNIFICANT_RETURN_BAND,
    classify_decision_quality,
    compute_outcome_scores,
)
from trader.domain.market_data import Bar

DEFAULT_FORWARD_SESSIONS = 5
DEFAULT_SHRINKAGE_K = 5.0
MIN_BENCH_EVALUATED = 8
SELECTION_SEMANTICS_VERSION = "bench_v2"
SELECTION_SEMANTICS_KEY = "selection_semantics_version"
_DEFAULT_VERDICT_BASIS = "direction"
DIRECTION_SOURCE_ALLOWED_SIDES = "allowed_sides"
DIRECTION_SOURCE_DIRECTIONAL_VIEW = "directional_view"
FLAIR_GROUP_ALLOCATION = "allocation"
FLAIR_GROUP_DIRECTION = "direction"
FLAIR_GROUP_DIRECTION_VIEW = "direction_view"
_DIRECTION_VIEW_ACTIONS = {"long_bias": "BUY", "short_bias": "SELL"}

__all__ = [
    "DEFAULT_FORWARD_SESSIONS",
    "DEFAULT_SHRINKAGE_K",
    "DIRECTION_SOURCE_ALLOWED_SIDES",
    "DIRECTION_SOURCE_DIRECTIONAL_VIEW",
    "FLAIR_GROUP_ALLOCATION",
    "FLAIR_GROUP_DIRECTION",
    "FLAIR_GROUP_DIRECTION_VIEW",
    "MIN_BENCH_EVALUATED",
    "SELECTION_SEMANTICS_KEY",
    "SELECTION_SEMANTICS_VERSION",
    "classify_allocation_quality",
    "classify_selection_quality",
    "direction_claim",
    "direction_source_of",
    "directional_action",
    "directional_view_action",
    "flair_scoring_group",
    "forward_return_over_sessions",
    "has_as_of_session",
    "is_live_feedback_row",
    "opportunity",
    "score_selection_outcomes",
    "to_flair_verdict",
    "verdict_basis_of",
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


def directional_view_action(directional_view: str | None) -> str | None:
    """Translate an advisory view into a scoring action, or ``None``."""
    view = str(directional_view or "").strip().lower()
    return _DIRECTION_VIEW_ACTIONS.get(view)


def direction_claim(
    allowed_sides: Sequence[str] | None,
    directional_view: str | None = None,
) -> tuple[str, str] | None:
    """Hard sides win. A directional view is a claim only when sides are not locked."""
    hard = directional_action(allowed_sides)
    if hard is not None:
        return hard, DIRECTION_SOURCE_ALLOWED_SIDES
    view = directional_view_action(directional_view)
    if view is not None:
        return view, DIRECTION_SOURCE_DIRECTIONAL_VIEW
    return None


def direction_source_of(row: Mapping[str, Any]) -> str:
    """Resolve the direction source; legacy NULL direction rows are hard sides."""
    source = str(row.get("direction_source") or "").strip()
    if source in {DIRECTION_SOURCE_ALLOWED_SIDES, DIRECTION_SOURCE_DIRECTIONAL_VIEW}:
        return source
    if verdict_basis_of(row) == "direction":
        return DIRECTION_SOURCE_ALLOWED_SIDES
    return ""


def _dated_bars(bars: Sequence[Bar]) -> list[tuple[datetime, Bar]]:
    dated = [(bar_ts, bar) for bar in bars if (bar_ts := _parse_ts(bar.ts)) is not None]
    dated.sort(key=lambda item: item[0])
    return dated


def _anchor_index(dated: Sequence[tuple[datetime, Bar]], as_of_ts: datetime) -> int | None:
    timestamps = [item[0] for item in dated]
    start = bisect_right(timestamps, as_of_ts) - 1
    if start < 0:
        start = bisect_left(timestamps, as_of_ts)
        if start >= len(dated):
            return None
    return start


def has_as_of_session(bars: Sequence[Bar], as_of: str) -> bool:
    """True when a dated session bar can anchor the as-of (horizon may still be open)."""
    as_of_ts = _parse_ts(as_of)
    if as_of_ts is None:
        return False
    dated = _dated_bars(bars)
    return bool(dated) and _anchor_index(dated, as_of_ts) is not None


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
    dated = _dated_bars(bars)
    if not dated:
        return None
    start = _anchor_index(dated, as_of_ts)
    if start is None:
        return None
    end = start + horizon_sessions
    if end >= len(dated):
        return None
    initial = dated[start][1].close
    if initial == 0.0:
        return None
    return dated[end][1].close / initial - 1.0


def opportunity(forward_return: float | None) -> float | None:
    """Absolute move used as allocation opportunity, or ``None`` if unknown."""
    if forward_return is None:
        return None
    return abs(float(forward_return))


def classify_allocation_quality(
    pick_opportunity: float | None,
    bench_opportunities: Sequence[float | None],
    band: float = SIGNIFICANT_RETURN_BAND,
) -> str:
    """Judge a pick against the bench median. Missing members are dropped, never zeroed."""
    if pick_opportunity is None:
        return "non_evaluable"
    evaluated = [float(value) for value in bench_opportunities if value is not None]
    if len(evaluated) < MIN_BENCH_EVALUATED:
        return "non_evaluable"
    excess = float(pick_opportunity) - float(median(evaluated))
    if excess > band:
        return "gagnant"
    if excess < -band:
        return "perdant"
    return "neutre"


def classify_selection_quality(
    allowed_sides: Sequence[str] | None,
    forward_return: float | None,
    *,
    band: float = SIGNIFICANT_RETURN_BAND,
    directional_view: str | None = None,
) -> str:
    """Verdict of one selection. Non-directional sides are ``non_evaluable``."""
    claim = direction_claim(allowed_sides, directional_view)
    if claim is None:
        return "non_evaluable"
    return classify_decision_quality(claim[0], forward_return, band)


def to_flair_verdict(verdict: str) -> str:
    """Map a French quality verdict onto the WIN/LOSS vocabulary FLAIR expects."""
    if verdict == "gagnant":
        return "WIN"
    if verdict == "perdant":
        return "LOSS"
    return verdict


def verdict_basis_of(row: Mapping[str, Any]) -> str:
    basis = str(row.get("verdict_basis") or "").strip()
    return basis or _DEFAULT_VERDICT_BASIS


def flair_scoring_group(row: Mapping[str, Any]) -> str:
    """FLAIR pool for one outcome row. Soft views never share the hard-direction rate."""
    basis = verdict_basis_of(row)
    if basis == "allocation":
        return FLAIR_GROUP_ALLOCATION
    if basis == "direction" and direction_source_of(row) == DIRECTION_SOURCE_DIRECTIONAL_VIEW:
        return FLAIR_GROUP_DIRECTION_VIEW
    return FLAIR_GROUP_DIRECTION


def is_live_feedback_row(row: Mapping[str, Any]) -> bool:
    """True for rows the universe agent may see (allocation + hard direction)."""
    return flair_scoring_group(row) != FLAIR_GROUP_DIRECTION_VIEW


def score_selection_outcomes(
    rows: Iterable[Mapping[str, Any]],
    *,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
) -> dict:
    """Run FLAIR per scoring group so allocation, hard direction and views stay separate."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[flair_scoring_group(row)].append(
            {
                "id": int(row["id"]),
                "symbol": row.get("symbol") or "",
                "family": row.get("family") or "",
                "verdict": to_flair_verdict(str(row.get("verdict") or "")),
            }
        )
    if not groups:
        return {"scored": 0, "base_rates": {}, "scores": {}}
    scores: dict[int, float] = {}
    base_rates: dict[str, dict[str, float]] = {}
    scored = 0
    for basis, mapped in groups.items():
        result = compute_outcome_scores(mapped, shrinkage_k=shrinkage_k)
        scores.update(result.get("scores") or {})
        base_rates[basis] = dict(result.get("base_rates") or {})
        scored += int(result.get("scored") or 0)
    return {"scored": scored, "base_rates": base_rates, "scores": scores}
