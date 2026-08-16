"""Evaluate situation notes via the market DataSource port and score them."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from trader.domain.market_data import MarketError
from trader.domain.semantic.catalog import FAMILIES, family_for_symbol
from trader.domain.situation.attribution import (
    DEFAULT_SHRINKAGE_K,
    classify_note_quality,
    directional_action,
    equal_weight_forward_return,
    horizon_bucket,
    parse_horizon_sessions,
    score_note_outcomes,
    targets_for_note,
)
from trader.market.protocols import DataSource

DEFAULT_BAR_INTERVAL = "1d"
DEFAULT_BAR_LOOKBACK = "1y"
MIN_FEEDBACK_N = 5
SITUATION_FEEDBACK_ROLE = "comparative_context_not_directives"

__all__ = [
    "DEFAULT_BAR_INTERVAL",
    "DEFAULT_BAR_LOOKBACK",
    "MIN_FEEDBACK_N",
    "SITUATION_FEEDBACK_ROLE",
    "EvaluatedNote",
    "SituationOutcomeStore",
    "evaluate_note",
    "evaluate_notes",
    "flair_identity",
    "persist_and_score",
    "refresh_situation_outcomes",
    "situation_feedback_digest",
    "summarize_outcomes",
]


@dataclass(frozen=True)
class EvaluatedNote:
    id: int
    section_type: str
    section_name: str
    direction: str
    as_of: str
    venue: str
    horizon_sessions: int | None
    forward_return: float | None
    coverage_n: int
    verdict: str
    symbol: str
    family: str


class SituationOutcomeStore(Protocol):
    def load_notes(self) -> list[Mapping[str, Any]]: ...

    def load_pending_notes(self, *, limit: int | None = None) -> list[Mapping[str, Any]]: ...

    def apply_outcomes(self, rows: Sequence[Mapping[str, Any]]) -> None: ...

    def load_outcomes(self) -> list[Mapping[str, Any]]: ...

    def update_outcome_scores(self, scores: Mapping[int, float]) -> None: ...

    def count(self) -> int: ...


def flair_identity(
    *,
    section_type: str,
    section_name: str,
    targets: Sequence[str],
) -> tuple[str, str]:
    """Return ``(symbol, family)`` used by ``compute_outcome_scores``."""
    if section_type == "family":
        family = section_name
        return family, family
    if section_type == "symbol":
        return section_name, family_for_symbol(section_name) or ""
    if section_type == "alert":
        if len(targets) == 1:
            ticker = targets[0]
            return ticker, family_for_symbol(ticker) or ""
        return "alert", ""
    return "", ""


def evaluate_note(
    note: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Sequence[Any]],
    *,
    families: Mapping[str, Sequence[str]] | None = None,
) -> EvaluatedNote | None:
    """Judge one note. Directional notes without a complete horizon are skipped."""
    catalog = families if families is not None else FAMILIES
    note_id = int(note["id"])
    section_type = str(note.get("section_type") or "")
    section_name = str(note.get("section_name") or "")
    direction = str(note.get("direction") or "")
    as_of = str(note.get("as_of") or "")
    venue = str(note.get("venue") or "")
    action = directional_action(direction)
    horizon = parse_horizon_sessions(note.get("horizon"), as_of=as_of)
    targets = targets_for_note(note, families=catalog)
    symbol, family = flair_identity(
        section_type=section_type,
        section_name=section_name,
        targets=targets,
    )

    if action is None or not targets or horizon is None:
        return EvaluatedNote(
            id=note_id,
            section_type=section_type,
            section_name=section_name,
            direction=direction,
            as_of=as_of,
            venue=venue,
            horizon_sessions=horizon,
            forward_return=None,
            coverage_n=0,
            verdict="non_evaluable",
            symbol=symbol,
            family=family,
        )

    forward, coverage = equal_weight_forward_return(
        bars_by_symbol,
        targets,
        as_of,
        horizon,
    )
    if forward is None:
        return None
    return EvaluatedNote(
        id=note_id,
        section_type=section_type,
        section_name=section_name,
        direction=direction,
        as_of=as_of,
        venue=venue,
        horizon_sessions=horizon,
        forward_return=forward,
        coverage_n=coverage,
        verdict=classify_note_quality(direction, forward),
        symbol=symbol,
        family=family,
    )


def evaluate_notes(
    notes: Iterable[Mapping[str, Any]],
    data_source: DataSource,
    *,
    lookback: str = DEFAULT_BAR_LOOKBACK,
    interval: str = DEFAULT_BAR_INTERVAL,
    families: Mapping[str, Sequence[str]] | None = None,
) -> list[EvaluatedNote]:
    """Fetch bars once per needed symbol through ``DataSource`` and evaluate."""
    catalog = families if families is not None else FAMILIES
    materialized = list(notes)
    needed: list[str] = []
    for note in materialized:
        if directional_action(note.get("direction")) is None:
            continue
        if parse_horizon_sessions(note.get("horizon"), as_of=note.get("as_of")) is None:
            continue
        needed.extend(targets_for_note(note, families=catalog))
    bars_by_symbol: dict[str, list[Any]] = {}
    for symbol in dict.fromkeys(needed):
        try:
            bars_by_symbol[symbol] = list(data_source.get_bars(symbol, lookback, interval))
        except MarketError:
            bars_by_symbol[symbol] = []
    evaluated: list[EvaluatedNote] = []
    for note in materialized:
        item = evaluate_note(note, bars_by_symbol, families=catalog)
        if item is not None:
            evaluated.append(item)
    return evaluated


def evaluated_to_row(item: EvaluatedNote, *, evaluated_at: str) -> dict[str, Any]:
    return {
        "id": item.id,
        "section_type": item.section_type,
        "section_name": item.section_name,
        "direction": item.direction,
        "as_of": item.as_of,
        "venue": item.venue,
        "horizon_sessions": item.horizon_sessions,
        "forward_return": item.forward_return,
        "coverage_n": item.coverage_n,
        "verdict": item.verdict,
        "symbol": item.symbol,
        "family": item.family,
        "evaluated_at": evaluated_at,
    }


def _rows_for_flair(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for row in rows:
        symbol, family = flair_identity(
            section_type=str(row.get("section_type") or ""),
            section_name=str(row.get("section_name") or ""),
            targets=targets_for_note(row, families=FAMILIES),
        )
        item = dict(row)
        item["symbol"] = row.get("symbol") or symbol
        item["family"] = row.get("family") or family
        mapped.append(item)
    return mapped


def persist_and_score(
    store: SituationOutcomeStore,
    evaluated: Sequence[EvaluatedNote],
    *,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
    now: datetime | None = None,
) -> dict:
    """Apply evaluated notes, then recompute FLAIR on the full stored set."""
    clock = now or datetime.now(timezone.utc)
    evaluated_at = clock.isoformat()
    if evaluated:
        store.apply_outcomes([evaluated_to_row(item, evaluated_at=evaluated_at) for item in evaluated])
    rows = _rows_for_flair(store.load_outcomes())
    result = score_note_outcomes(rows, shrinkage_k=shrinkage_k)
    store.update_outcome_scores(result.get("scores") or {})
    return {
        "scored": result.get("scored", 0),
        "base_rates": result.get("base_rates", {}),
        "n_stored": len(rows),
    }


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _group_summary(rows: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "")].append(row)
    summaries: list[dict[str, Any]] = []
    for name, items in groups.items():
        n_gagnant = sum(1 for row in items if row.get("verdict") == "gagnant")
        n_perdant = sum(1 for row in items if row.get("verdict") == "perdant")
        n_neutre = sum(1 for row in items if row.get("verdict") == "neutre")
        n_non_evaluable = sum(1 for row in items if row.get("verdict") == "non_evaluable")
        directional = n_gagnant + n_perdant
        returns = [
            float(row["forward_return"])
            for row in items
            if row.get("forward_return") is not None
        ]
        scores = [
            float(row["outcome_score"])
            for row in items
            if row.get("outcome_score") is not None
        ]
        summaries.append(
            {
                key: name,
                "n": len(items),
                "n_gagnant": n_gagnant,
                "n_perdant": n_perdant,
                "n_neutre": n_neutre,
                "n_non_evaluable": n_non_evaluable,
                "win_rate": (n_gagnant / directional) if directional else None,
                "mean_forward_return": _mean(returns),
                "mean_outcome_score": _mean(scores),
            }
        )
    summaries.sort(
        key=lambda item: (
            item["mean_outcome_score"] is None,
            -(item["mean_outcome_score"] or 0.0),
            item["win_rate"] is None,
            -(item["win_rate"] or 0.0),
            -int(item["n"]),
            str(item[key]),
        )
    )
    return summaries


def _split_pays_decoit(groups: Sequence[Mapping[str, Any]], key: str) -> dict[str, list[str]]:
    pays: list[str] = []
    decoit: list[str] = []
    for item in groups:
        name = str(item.get(key) or "")
        if not name:
            continue
        score = item.get("mean_outcome_score")
        win_rate = item.get("win_rate")
        if score is not None and score > 0.0:
            pays.append(name)
        elif score is not None and score < 0.0:
            decoit.append(name)
        elif win_rate is not None and win_rate > 0.5:
            pays.append(name)
        elif win_rate is not None and win_rate < 0.5:
            decoit.append(name)
    return {"pays": pays, "decoit": decoit}


def _with_derived(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["horizon_bucket"] = horizon_bucket(
        None if row.get("horizon_sessions") is None else int(row["horizon_sessions"])
    )
    if not payload.get("family"):
        if payload.get("section_type") == "family":
            payload["family"] = str(payload.get("section_name") or "")
        elif payload.get("section_type") == "symbol":
            payload["family"] = family_for_symbol(str(payload.get("section_name") or "")) or ""
    return payload


def _directional_n(item: Mapping[str, Any]) -> int:
    return int(item.get("n_gagnant") or 0) + int(item.get("n_perdant") or 0)


def _group_utility(item: Mapping[str, Any]) -> str:
    score = item.get("mean_outcome_score")
    if score is not None and float(score) > 0.0:
        return "helps"
    if score is not None and float(score) < 0.0:
        return "hurts"
    win_rate = item.get("win_rate")
    if win_rate is not None and float(win_rate) > 0.5:
        return "helps"
    if win_rate is not None and float(win_rate) < 0.5:
        return "hurts"
    return "neutral"


def _feedback_rows(
    groups: Sequence[Mapping[str, Any]],
    key: str,
    *,
    min_n: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in groups:
        name = str(item.get(key) or "")
        directional = _directional_n(item)
        if not name or directional < min_n:
            continue
        rows.append(
            {
                key: name,
                "n": directional,
                "win_rate": item.get("win_rate"),
                "mean_outcome_score": item.get("mean_outcome_score"),
                "utility": _group_utility(item),
            }
        )
    return rows


def situation_feedback_digest(
    rows: Sequence[Mapping[str, Any]],
    *,
    venue: str | None = None,
    min_n: int = MIN_FEEDBACK_N,
) -> dict[str, Any]:
    """Compact market-as-judge digest for the news/macro analyst. Never directives."""

    scoped = [
        row
        for row in rows
        if venue is None or str(row.get("venue") or "").strip().upper() == str(venue).strip().upper()
    ]
    summary = summarize_outcomes(scoped)
    directions = _feedback_rows(summary["by_direction"], "direction", min_n=min_n)
    section_types = _feedback_rows(summary["by_section_type"], "section_type", min_n=min_n)
    families = _feedback_rows(summary["by_family"], "family", min_n=min_n)
    directional = int(summary["n_gagnant"]) + int(summary["n_perdant"])
    return {
        "role": SITUATION_FEEDBACK_ROLE,
        "status": "observed" if directions or section_types or families else "insufficient",
        "min_n": min_n,
        "n_evaluated": directional,
        "n_non_evaluable": int(summary["n_non_evaluable"]),
        "directions": directions,
        "section_types": section_types,
        "families": families,
    }


def refresh_situation_outcomes(
    store: SituationOutcomeStore,
    data_source: DataSource,
    *,
    limit: int = 128,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
    now: datetime | None = None,
    families: Mapping[str, Sequence[str]] | None = None,
    lookback: str = DEFAULT_BAR_LOOKBACK,
    interval: str = DEFAULT_BAR_INTERVAL,
) -> dict[str, Any]:
    """Score a bounded batch of still-unjudged situation notes. Fail-open caller."""

    cap = max(0, int(limit))
    pending = list(store.load_pending_notes())
    evaluated: list[EvaluatedNote] = []
    page = max(cap, 1)
    for start in range(0, len(pending), page):
        if cap == 0 or len(evaluated) >= cap:
            break
        judged = evaluate_notes(
            pending[start : start + page],
            data_source,
            lookback=lookback,
            interval=interval,
            families=families,
        )
        evaluated.extend(judged)
    if cap:
        evaluated = evaluated[:cap]
    persist_and_score(store, evaluated, shrinkage_k=shrinkage_k, now=now)
    return {
        "pending": len(pending),
        "evaluated": len(evaluated),
        "stored": store.count(),
    }


def summarize_outcomes(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """JSON-ready answer: which directions / families / horizons predict."""
    derived = [_with_derived(row) for row in rows]
    n_gagnant = sum(1 for row in derived if row.get("verdict") == "gagnant")
    n_perdant = sum(1 for row in derived if row.get("verdict") == "perdant")
    n_neutre = sum(1 for row in derived if row.get("verdict") == "neutre")
    n_non_evaluable = sum(1 for row in derived if row.get("verdict") == "non_evaluable")
    by_direction = _group_summary(derived, "direction")
    by_family = _group_summary(derived, "family")
    by_horizon = _group_summary(derived, "horizon_bucket")
    by_section_type = _group_summary(derived, "section_type")
    return {
        "n": len(derived),
        "n_gagnant": n_gagnant,
        "n_perdant": n_perdant,
        "n_neutre": n_neutre,
        "n_non_evaluable": n_non_evaluable,
        "by_direction": by_direction,
        "by_family": by_family,
        "by_horizon": by_horizon,
        "by_section_type": by_section_type,
        "pays": {
            "directions": _split_pays_decoit(by_direction, "direction")["pays"],
            "families": _split_pays_decoit(by_family, "family")["pays"],
            "horizons": _split_pays_decoit(by_horizon, "horizon_bucket")["pays"],
        },
        "decoit": {
            "directions": _split_pays_decoit(by_direction, "direction")["decoit"],
            "families": _split_pays_decoit(by_family, "family")["decoit"],
            "horizons": _split_pays_decoit(by_horizon, "horizon_bucket")["decoit"],
        },
    }
