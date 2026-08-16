"""Pool de candidats borné et pondéré par l'outcome pour un cycle de consolidation.

In : notes brutes nouvelles + lignes d'historique scorées optionnelles.
Out : au plus 80 candidats uniques (50 récents, 15 confirmations, 15 contre-exemples).
Invariant : ranking déterministe ; cap doux par symbole avant remplissage ; pas d'I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from trader.domain.learnings.selection import parse_ts

DEFAULT_MAX_RECENT_CANDIDATES = 50
DEFAULT_MAX_CONFIRMATION_CANDIDATES = 15
DEFAULT_MAX_COUNTEREXAMPLE_CANDIDATES = 15
DEFAULT_MAX_CURATION_CANDIDATES = (
    DEFAULT_MAX_RECENT_CANDIDATES
    + DEFAULT_MAX_CONFIRMATION_CANDIDATES
    + DEFAULT_MAX_COUNTEREXAMPLE_CANDIDATES
)
DEFAULT_SOFT_MAX_CANDIDATES_PER_SYMBOL = 12

__all__ = [
    "DEFAULT_MAX_CONFIRMATION_CANDIDATES",
    "DEFAULT_MAX_COUNTEREXAMPLE_CANDIDATES",
    "DEFAULT_MAX_CURATION_CANDIDATES",
    "DEFAULT_MAX_RECENT_CANDIDATES",
    "DEFAULT_SOFT_MAX_CANDIDATES_PER_SYMBOL",
    "build_curation_candidates",
    "candidate_id",
]


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def candidate_id(row: dict, *, index: int) -> str:
    """Id source : ``id``/``note_id``, sinon ``decision_id``, sinon ts/symbole/index."""

    raw_id = row.get("id")
    if raw_id is None:
        raw_id = row.get("note_id")
    if raw_id is not None and str(raw_id).strip():
        return str(raw_id).strip()
    decision_id = str(row.get("decision_id") or "").strip()
    if decision_id:
        return decision_id
    return "legacy:{}:{}:{}".format(
        row.get("ts") or "unknown-ts",
        row.get("symbol") or "unknown-symbol",
        index,
    )


def _candidate_identity(row: dict, *, index: int) -> str:
    """Clé de dédup : ``decision:<id>`` si présent, sinon ``candidate:<id>``."""

    decision_id = str(row.get("decision_id") or "").strip()
    if decision_id:
        return f"decision:{decision_id}"
    return f"candidate:{candidate_id(row, index=index)}"


def _candidate_status(row: dict) -> str:
    """``evaluated`` si WIN/LOSS/NEUTRAL, sinon ``pending``."""

    verdict = str(row.get("verdict") or "").upper()
    return "evaluated" if verdict in {"WIN", "LOSS", "NEUTRAL"} else "pending"


def _candidate_from_row(row: dict, *, index: int) -> dict | None:
    """Projette une ligne en candidat, ou None si la note est vide."""

    note = str(row.get("note") or row.get("text") or "").strip()
    if not note:
        return None
    verdict = str(row.get("verdict") or "UNKNOWN").upper()
    if verdict not in {"WIN", "LOSS", "NEUTRAL", "UNKNOWN"}:
        verdict = "UNKNOWN"
    return {
        "id": candidate_id(row, index=index),
        "identity": _candidate_identity(row, index=index),
        "ts": str(row.get("ts") or ""),
        "symbol": str(row.get("symbol") or ""),
        "family": row.get("family"),
        "decision_id": row.get("decision_id"),
        "action": row.get("action"),
        "intent": row.get("intent"),
        "executed": row.get("executed"),
        "note": note,
        "feedback": {
            "status": _candidate_status(row),
            "verdict": verdict,
            "reward": _as_float(row.get("reward")),
            "forward_return": _as_float(row.get("forward_return")),
            "outcome_score": _as_float(row.get("outcome_score")),
            "q_value": _as_float(row.get("q_value")),
            "q_updates": _nonnegative_int(row.get("q_updates")),
        },
    }


def _candidate_time(candidate: dict) -> datetime:
    return parse_ts(candidate.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)


def _candidate_quality(candidate: dict) -> float:
    """FLAIR + 0.2 × Q × q_updates/(q_updates+5)."""

    feedback = candidate["feedback"]
    flair = float(feedback.get("outcome_score") or 0.0)
    q_value = float(feedback.get("q_value") or 0.0)
    q_updates = _nonnegative_int(feedback.get("q_updates"))
    q_confidence = q_updates / (q_updates + 5.0)
    return flair + 0.2 * q_value * q_confidence


def _append_with_soft_symbol_cap(
    selected: list[dict],
    rows: Iterable[dict],
    *,
    limit: int,
    seen: set[str],
    symbol_counts: dict[str, int],
) -> None:
    """Ajoute une catégorie classée : cap symbole d'abord, puis remplissage."""

    candidates = list(rows)
    deferred: list[dict] = []
    for candidate in candidates:
        if len(selected) >= limit:
            return
        identity = candidate["identity"]
        if identity in seen:
            continue
        symbol = candidate["symbol"]
        if symbol and symbol_counts.get(symbol, 0) >= DEFAULT_SOFT_MAX_CANDIDATES_PER_SYMBOL:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        seen.add(identity)
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
    for candidate in deferred:
        if len(selected) >= limit:
            return
        identity = candidate["identity"]
        if identity in seen:
            continue
        selected.append(candidate)
        seen.add(identity)
        symbol = candidate["symbol"]
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1


def build_curation_candidates(
    new_raw: list[dict],
    *,
    candidate_rows: list[dict] | None = None,
) -> list[dict]:
    """Pool 50 récents + 15 confirmations + 15 contre-exemples.

    Les bruts nouveaux sont toujours des candidats provisoires. L'historique
    peut enrichir ou étendre le pool. Sortie sans clés internes ``identity`` / ``_enriched``.
    """

    recent_candidates = [
        candidate
        for index, row in enumerate(new_raw)
        if (candidate := _candidate_from_row(row, index=index)) is not None
    ]
    history_candidates = [
        candidate
        for index, row in enumerate(candidate_rows or [])
        if (candidate := _candidate_from_row(row, index=index + len(new_raw))) is not None
    ]
    for candidate in recent_candidates:
        candidate["_enriched"] = False
    for candidate in history_candidates:
        candidate["_enriched"] = True

    selected: list[dict] = []
    seen: set[str] = set()
    symbol_counts: dict[str, int] = {}
    _append_with_soft_symbol_cap(
        selected,
        sorted(
            recent_candidates + history_candidates,
            key=lambda candidate: (_candidate_time(candidate), bool(candidate["_enriched"])),
            reverse=True,
        ),
        limit=DEFAULT_MAX_RECENT_CANDIDATES,
        seen=seen,
        symbol_counts=symbol_counts,
    )

    all_candidates = history_candidates + recent_candidates
    confirmations = sorted(
        (
            candidate
            for candidate in all_candidates
            if candidate["feedback"]["verdict"] == "WIN" or _candidate_quality(candidate) > 0.0
        ),
        key=lambda candidate: (_candidate_quality(candidate), _candidate_time(candidate)),
        reverse=True,
    )
    _append_with_soft_symbol_cap(
        selected,
        confirmations,
        limit=DEFAULT_MAX_RECENT_CANDIDATES + DEFAULT_MAX_CONFIRMATION_CANDIDATES,
        seen=seen,
        symbol_counts=symbol_counts,
    )

    counterexamples = sorted(
        (
            candidate
            for candidate in all_candidates
            if candidate["feedback"]["verdict"] == "LOSS" or _candidate_quality(candidate) < 0.0
        ),
        key=lambda candidate: (_candidate_quality(candidate), _candidate_time(candidate)),
    )
    _append_with_soft_symbol_cap(
        selected,
        counterexamples,
        limit=DEFAULT_MAX_CURATION_CANDIDATES,
        seen=seen,
        symbol_counts=symbol_counts,
    )

    return [
        {key: value for key, value in candidate.items() if key not in {"identity", "_enriched"}}
        for candidate in selected
    ]
