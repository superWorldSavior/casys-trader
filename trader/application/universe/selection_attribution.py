"""Evaluate universe selections via the market DataSource port and score them."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from trader.domain.market_data import MarketError
from trader.domain.universe.intelligence import UNCLASSIFIED_FAMILY
from trader.domain.universe.selection_attribution import (
    DEFAULT_FORWARD_SESSIONS,
    DEFAULT_SHRINKAGE_K,
    classify_allocation_quality,
    classify_selection_quality,
    directional_action,
    forward_return_over_sessions,
    has_as_of_session,
    opportunity,
    score_selection_outcomes,
)
from trader.market.protocols import DataSource

DEFAULT_BAR_INTERVAL = "1d"
DEFAULT_BAR_LOOKBACK = "1y"
MIN_FEEDBACK_N = 5
SELECTION_FEEDBACK_ROLE = "comparative_context_not_hotlist"

__all__ = [
    "DEFAULT_BAR_INTERVAL",
    "DEFAULT_BAR_LOOKBACK",
    "MIN_FEEDBACK_N",
    "SELECTION_FEEDBACK_ROLE",
    "EvaluatedSelection",
    "CandidateScopeReader",
    "CandidateScopeStoreOpener",
    "SelectionOutcomeStore",
    "SelectionOutcomeStoreOpener",
    "UniverseSelection",
    "compare_selection_selectors",
    "evaluate_selection",
    "evaluate_selections",
    "iter_mandate_payloads",
    "load_mandate_selections",
    "persist_and_score",
    "refresh_selection_outcomes",
    "resolve_bench",
    "selection_feedback_digest",
    "selections_from_mandate_payload",
    "selections_from_mandate_payloads",
    "summarize_outcomes",
]

_JUDGED_STATUSES = frozenset({"active", "fallback"})


@dataclass(frozen=True)
class UniverseSelection:
    mandate_id: str
    symbol: str
    role: str
    allowed_sides: tuple[str, ...]
    as_of: str
    venue: str = ""
    family: str = UNCLASSIFIED_FAMILY
    candidate_scope_id: str = ""
    status: str = ""
    selector: str = "agent"
    selected_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluatedSelection:
    mandate_id: str
    symbol: str
    role: str
    allowed_sides: tuple[str, ...]
    as_of: str
    venue: str
    family: str
    horizon_sessions: int
    forward_return: float | None
    verdict: str
    verdict_basis: str = "direction"
    selector: str = "agent"
    candidate_scope_id: str = ""
    opportunity: float | None = None
    bench_median_opportunity: float | None = None
    allocation_excess: float | None = None
    bench_n: int | None = None
    scope_missing: bool = False


class SelectionOutcomeStore(Protocol):
    def upsert_outcomes(self, rows: Sequence[Mapping[str, Any]]) -> None: ...

    def load_outcomes(self) -> list[Mapping[str, Any]]: ...

    def update_flair_scores(self, scores: Mapping[int, float]) -> None: ...

    def count(self) -> int: ...

    def ensure_selection_semantics(self) -> str: ...


class SelectionOutcomeStoreOpener(Protocol):
    def __call__(self, state_dir: str | Path) -> SelectionOutcomeStore: ...


class CandidateScopeReader(Protocol):
    def read_by_id(
        self,
        candidate_scope_id: str,
        hint_date: str | None = None,
    ) -> Mapping[str, Any] | None: ...


class CandidateScopeStoreOpener(Protocol):
    def __call__(self, state_dir: str | Path) -> CandidateScopeReader: ...


def _allowed_sides(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(str(side).strip() for side in raw if str(side).strip() in {"long", "short"})
    )


def _family_of(symbol_mandate: Mapping[str, Any]) -> str:
    context = symbol_mandate.get("family_context")
    if isinstance(context, Mapping):
        name = str(context.get("family") or "").strip()
        if name:
            return name
    return UNCLASSIFIED_FAMILY


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload_status(payload: Mapping[str, Any]) -> str:
    status = str(payload.get("status") or "").strip().lower()
    if status:
        return status
    return str(_mapping(payload.get("mandate_ref")).get("status") or "").strip().lower()


def _payload_scope_id(payload: Mapping[str, Any]) -> str:
    scope_id = str(payload.get("candidate_scope_id") or "").strip()
    if scope_id:
        return scope_id
    return str(_mapping(payload.get("mandate_ref")).get("candidate_scope_id") or "").strip()


def _payload_fallback_reason(payload: Mapping[str, Any]) -> object:
    reason = payload.get("fallback_reason")
    if reason not in (None, ""):
        return reason
    return _mapping(payload.get("mandate_ref")).get("fallback_reason")


def _is_judged_status(status: str) -> bool:
    # Live history 2026-08-16: 0/476 lines omit status. Missing status is a
    # legacy activation (or an active slice), never a prepared proposal.
    if not status:
        return True
    return status in _JUDGED_STATUSES


def _selector_for(*, status: str, fallback_reason: object) -> str:
    if status == "fallback" or fallback_reason not in (None, ""):
        return "baseline_fallback"
    return "agent"


def _selection(
    *,
    mandate_id: str,
    symbol: str,
    symbol_mandate: Mapping[str, Any],
    as_of: str,
    venue: str,
    candidate_scope_id: str = "",
    status: str = "",
    selector: str = "agent",
    selected_symbols: tuple[str, ...] = (),
) -> UniverseSelection | None:
    if not mandate_id or not symbol or not as_of:
        return None
    picks = selected_symbols or (symbol,)
    return UniverseSelection(
        mandate_id=mandate_id,
        symbol=symbol,
        role=str(symbol_mandate.get("role") or "").strip(),
        allowed_sides=_allowed_sides(symbol_mandate.get("allowed_sides")),
        as_of=as_of,
        venue=venue,
        family=_family_of(symbol_mandate),
        candidate_scope_id=candidate_scope_id,
        status=status,
        selector=selector,
        selected_symbols=picks,
    )


def selections_from_mandate_payload(payload: Mapping[str, Any]) -> list[UniverseSelection]:
    """Project one judged mandate snapshot (history line or active slice)."""
    status = _payload_status(payload)
    if not _is_judged_status(status):
        return []
    scope_id = _payload_scope_id(payload)
    selector = _selector_for(status=status, fallback_reason=_payload_fallback_reason(payload))
    symbols = payload.get("symbols")
    if isinstance(symbols, Mapping):
        mandate_id = str(payload.get("mandate_id") or "")
        as_of = str(payload.get("as_of") or "")
        venue = str(payload.get("venue") or "")
        selected_symbols = tuple(
            dict.fromkeys(
                str(raw.get("symbol") or symbol).strip()
                for symbol, raw in symbols.items()
                if isinstance(raw, Mapping) and str(raw.get("symbol") or symbol).strip()
            )
        )
        out: list[UniverseSelection] = []
        for symbol, raw in symbols.items():
            if not isinstance(raw, Mapping):
                continue
            item = _selection(
                mandate_id=mandate_id,
                symbol=str(raw.get("symbol") or symbol).strip(),
                symbol_mandate=raw,
                as_of=as_of,
                venue=venue,
                candidate_scope_id=scope_id,
                status=status,
                selector=selector,
                selected_symbols=selected_symbols,
            )
            if item is not None:
                out.append(item)
        return out

    ref = _mapping(payload.get("mandate_ref"))
    symbol_mandate = payload.get("symbol_mandate")
    if not isinstance(symbol_mandate, Mapping):
        return []
    item = _selection(
        mandate_id=str(ref.get("mandate_id") or ""),
        symbol=str(symbol_mandate.get("symbol") or "").strip(),
        symbol_mandate=symbol_mandate,
        as_of=str(ref.get("as_of") or payload.get("as_of") or ""),
        venue=str(ref.get("venue") or payload.get("venue") or ""),
        candidate_scope_id=scope_id,
        status=status,
        selector=selector,
    )
    return [item] if item is not None else []


def selections_from_mandate_payloads(
    payloads: Iterable[Mapping[str, Any]],
) -> list[UniverseSelection]:
    """Judge activations only; collapse fallback bursts by (scope, symbol)."""
    out: list[UniverseSelection] = []
    seen_agent: set[tuple[str, str, str]] = set()
    seen_fallback: set[tuple[str, str]] = set()
    for payload in payloads:
        for selection in selections_from_mandate_payload(payload):
            if selection.selector == "baseline_fallback":
                key_fb = (selection.candidate_scope_id, selection.symbol)
                if key_fb in seen_fallback:
                    continue
                seen_fallback.add(key_fb)
            else:
                key_ag = (selection.mandate_id, selection.symbol, selection.as_of)
                if key_ag in seen_agent:
                    continue
                seen_agent.add(key_ag)
            out.append(selection)
    return out


def resolve_bench(selection: UniverseSelection, scope: Mapping[str, Any]) -> list[str]:
    """Candidates minus mandate picks minus sticky. Order follows ``scope.candidates``."""
    picks = {str(symbol).strip() for symbol in selection.selected_symbols if str(symbol).strip()}
    if selection.symbol.strip():
        picks.add(selection.symbol.strip())
    sticky = {
        str(symbol).strip()
        for symbol in (scope.get("sticky_context_at_close") or ())
        if str(symbol).strip()
    }
    bench: list[str] = []
    seen: set[str] = set()
    for raw in scope.get("candidates") or ():
        if isinstance(raw, Mapping):
            symbol = str(raw.get("symbol") or "").strip()
        else:
            symbol = str(raw or "").strip()
        if not symbol or symbol in picks or symbol in sticky or symbol in seen:
            continue
        seen.add(symbol)
        bench.append(symbol)
    return bench


def _is_pending(bars: Sequence[Any], as_of: str, horizon_sessions: int) -> bool:
    return has_as_of_session(bars, as_of) and forward_return_over_sessions(bars, as_of, horizon_sessions) is None


def _evaluated(
    selection: UniverseSelection,
    *,
    horizon_sessions: int,
    forward_return: float | None,
    verdict: str,
    verdict_basis: str,
    opportunity_value: float | None = None,
    bench_median_opportunity: float | None = None,
    allocation_excess: float | None = None,
    bench_n: int | None = None,
    scope_missing: bool = False,
) -> EvaluatedSelection:
    return EvaluatedSelection(
        mandate_id=selection.mandate_id,
        symbol=selection.symbol,
        role=selection.role,
        allowed_sides=selection.allowed_sides,
        as_of=selection.as_of,
        venue=selection.venue,
        family=selection.family,
        horizon_sessions=horizon_sessions,
        forward_return=forward_return,
        verdict=verdict,
        verdict_basis=verdict_basis,
        selector=selection.selector,
        candidate_scope_id=selection.candidate_scope_id,
        opportunity=opportunity_value,
        bench_median_opportunity=bench_median_opportunity,
        allocation_excess=allocation_excess,
        bench_n=bench_n,
        scope_missing=scope_missing,
    )


def evaluate_selection(
    selection: UniverseSelection,
    bars: Sequence[Any],
    *,
    horizon_sessions: int = DEFAULT_FORWARD_SESSIONS,
    bench_opportunities: Sequence[float | None] | None = None,
    bench_unresolved: bool = False,
) -> list[EvaluatedSelection]:
    """Judge one selection on both bases. Empty means pending — do not persist."""
    forward = forward_return_over_sessions(bars, selection.as_of, horizon_sessions)
    if has_as_of_session(bars, selection.as_of) and forward is None:
        return []
    rows: list[EvaluatedSelection] = []
    if directional_action(selection.allowed_sides) is not None and forward is not None:
        rows.append(
            _evaluated(
                selection,
                horizon_sessions=horizon_sessions,
                forward_return=forward,
                verdict=classify_selection_quality(selection.allowed_sides, forward),
                verdict_basis="direction",
            )
        )
    pick_opp = opportunity(forward)
    if bench_unresolved:
        rows.append(
            _evaluated(
                selection,
                horizon_sessions=horizon_sessions,
                forward_return=forward,
                verdict="non_evaluable",
                verdict_basis="allocation",
                opportunity_value=pick_opp,
                bench_n=0,
                scope_missing=True,
            )
        )
        return rows
    bench = list(bench_opportunities or ())
    evaluated_bench = [float(value) for value in bench if value is not None]
    median_opp = float(median(evaluated_bench)) if evaluated_bench else None
    excess = None if pick_opp is None or median_opp is None else pick_opp - median_opp
    rows.append(
        _evaluated(
            selection,
            horizon_sessions=horizon_sessions,
            forward_return=forward,
            verdict=classify_allocation_quality(pick_opp, bench),
            verdict_basis="allocation",
            opportunity_value=pick_opp,
            bench_median_opportunity=median_opp,
            allocation_excess=excess,
            bench_n=len(evaluated_bench),
        )
    )
    return rows


def _candidate_symbols(scope: Mapping[str, Any]) -> list[str]:
    sticky = {
        str(symbol).strip()
        for symbol in (scope.get("sticky_context_at_close") or ())
        if str(symbol).strip()
    }
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in scope.get("candidates") or ():
        if isinstance(raw, Mapping):
            symbol = str(raw.get("symbol") or "").strip()
        else:
            symbol = str(raw or "").strip()
        if not symbol or symbol in sticky or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def evaluate_selections(
    selections: Iterable[UniverseSelection],
    data_source: DataSource,
    *,
    horizon_sessions: int = DEFAULT_FORWARD_SESSIONS,
    lookback: str = DEFAULT_BAR_LOOKBACK,
    interval: str = DEFAULT_BAR_INTERVAL,
    scope_reader: CandidateScopeReader | None = None,
) -> list[EvaluatedSelection]:
    """Fetch bars once per symbol through ``DataSource`` and evaluate each selection."""
    materialized = list(selections)
    bars_by_symbol: dict[str, list[Any]] = {}

    failed_symbols: set[str] = set()

    def bars_for(symbol: str) -> list[Any]:
        if symbol in failed_symbols:
            return []
        if symbol not in bars_by_symbol:
            try:
                bars_by_symbol[symbol] = list(data_source.get_bars(symbol, lookback, interval))
            except MarketError:
                failed_symbols.add(symbol)
                bars_by_symbol[symbol] = []
        return bars_by_symbol[symbol]

    for symbol in dict.fromkeys(item.symbol for item in materialized):
        bars_for(symbol)

    scope_cache: dict[str, Mapping[str, Any] | None] = {}
    prefetched: set[str] = set()
    evaluated: list[EvaluatedSelection] = []

    def load_scope(selection: UniverseSelection) -> Mapping[str, Any] | None:
        scope_id = selection.candidate_scope_id
        if not scope_id:
            return None
        if scope_id in scope_cache:
            return scope_cache[scope_id]
        if scope_reader is None:
            scope_cache[scope_id] = None
            return None
        hint = selection.as_of[:10] if len(selection.as_of) >= 10 else None
        scope = scope_reader.read_by_id(scope_id, hint_date=hint)
        scope_cache[scope_id] = scope
        return scope

    for selection in materialized:
        if selection.symbol in failed_symbols:
            continue
        pick_bars = bars_for(selection.symbol)
        if _is_pending(pick_bars, selection.as_of, horizon_sessions):
            continue
        scope = load_scope(selection)
        if scope is None:
            evaluated.extend(
                evaluate_selection(
                    selection,
                    pick_bars,
                    horizon_sessions=horizon_sessions,
                    bench_unresolved=True,
                )
            )
            continue
        scope_id = str(scope.get("candidate_scope_id") or selection.candidate_scope_id)
        if scope_id not in prefetched:
            for symbol in _candidate_symbols(scope):
                bars_for(symbol)
            prefetched.add(scope_id)
        bench_opportunities = [
            opportunity(forward_return_over_sessions(bars_for(symbol), selection.as_of, horizon_sessions))
            for symbol in resolve_bench(selection, scope)
        ]
        evaluated.extend(
            evaluate_selection(
                selection,
                pick_bars,
                horizon_sessions=horizon_sessions,
                bench_opportunities=bench_opportunities,
            )
        )
    return evaluated


def evaluated_to_row(item: EvaluatedSelection, *, evaluated_at: str) -> dict[str, Any]:
    return {
        "mandate_id": item.mandate_id,
        "symbol": item.symbol,
        "family": item.family,
        "role": item.role,
        "allowed_sides": list(item.allowed_sides),
        "as_of": item.as_of,
        "venue": item.venue,
        "horizon_sessions": item.horizon_sessions,
        "forward_return": item.forward_return,
        "verdict": item.verdict,
        "evaluated_at": evaluated_at,
        "verdict_basis": item.verdict_basis,
        "candidate_scope_id": item.candidate_scope_id,
        "selector": item.selector,
        "opportunity": item.opportunity,
        "bench_median_opportunity": item.bench_median_opportunity,
        "allocation_excess": item.allocation_excess,
        "bench_n": item.bench_n,
    }


def persist_and_score(
    store: SelectionOutcomeStore,
    evaluated: Sequence[EvaluatedSelection],
    *,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
    now: datetime | None = None,
) -> dict:
    """Upsert evaluated selections, then recompute FLAIR on the full stored set."""
    clock = now or datetime.now(timezone.utc)
    evaluated_at = clock.isoformat()
    if evaluated:
        store.upsert_outcomes([evaluated_to_row(item, evaluated_at=evaluated_at) for item in evaluated])
    rows = store.load_outcomes()
    scores: dict[int, float] = {}
    base_rates: dict[str, dict] = {}
    scored = 0
    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        bucket = "agent" if _is_agent_selector(row) else "baseline"
        buckets[bucket].append(row)
    for bucket, group in buckets.items():
        result = score_selection_outcomes(group, shrinkage_k=shrinkage_k)
        scores.update(result.get("scores") or {})
        base_rates[bucket] = result.get("base_rates") or {}
        scored += int(result.get("scored") or 0)
    store.update_flair_scores(scores)
    return {
        "scored": scored,
        "base_rates": base_rates,
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
        scores = [float(row["flair_score"]) for row in items if row.get("flair_score") is not None]
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
                "mean_flair_score": _mean(scores),
            }
        )
    summaries.sort(
        key=lambda item: (
            item["mean_flair_score"] is None,
            -(item["mean_flair_score"] or 0.0),
            item["win_rate"] is None,
            -(item["win_rate"] or 0.0),
            -int(item["n"]),
            str(item[key]),
        )
    )
    return summaries


def _directional_n(item: Mapping[str, Any]) -> int:
    return int(item.get("n_gagnant") or 0) + int(item.get("n_perdant") or 0)


def _split_pays_decoit(
    groups: Sequence[Mapping[str, Any]],
    key: str,
    *,
    min_n: int = MIN_FEEDBACK_N,
) -> dict[str, list[str]]:
    pays: list[str] = []
    decoit: list[str] = []
    for item in groups:
        name = str(item.get(key) or "")
        if not name or _directional_n(item) < min_n:
            continue
        score = item.get("mean_flair_score")
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


def _group_utility(item: Mapping[str, Any]) -> str:
    score = item.get("mean_flair_score")
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


def _is_agent_selector(row: Mapping[str, Any]) -> bool:
    selector = str(row.get("selector") or "").strip()
    return selector in {"", "agent"}


def _verdict_basis_of(row: Mapping[str, Any]) -> str:
    basis = str(row.get("verdict_basis") or "").strip()
    return basis or "direction"


def _basis_block(
    items: Sequence[Mapping[str, Any]],
    *,
    rate_key: str,
    min_n: int,
) -> dict[str, Any] | None:
    n_gagnant = sum(1 for row in items if row.get("verdict") == "gagnant")
    n_perdant = sum(1 for row in items if row.get("verdict") == "perdant")
    decisive = n_gagnant + n_perdant
    if decisive < min_n:
        return None
    rate = n_gagnant / decisive
    scores = [float(row["flair_score"]) for row in items if row.get("flair_score") is not None]
    mean_score = _mean(scores)
    return {
        "n": decisive,
        rate_key: rate,
        "mean_flair_score": mean_score,
        "utility": _group_utility({"mean_flair_score": mean_score, "win_rate": rate}),
    }


def _two_basis_feedback(
    rows: Sequence[Mapping[str, Any]],
    key: str,
    *,
    min_n: int,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: {"allocation": [], "direction": []}
    )
    for row in rows:
        name = str(row.get(key) or "")
        basis = _verdict_basis_of(row)
        if not name or basis not in {"allocation", "direction"}:
            continue
        groups[name][basis].append(row)
    feedback: list[dict[str, Any]] = []
    for name, by_basis in groups.items():
        entry: dict[str, Any] = {key: name}
        allocation = _basis_block(by_basis["allocation"], rate_key="beat_bench_rate", min_n=min_n)
        direction = _basis_block(by_basis["direction"], rate_key="win_rate", min_n=min_n)
        if allocation is not None:
            entry["allocation"] = allocation
        if direction is not None:
            entry["direction"] = direction
        if "allocation" in entry or "direction" in entry:
            feedback.append(entry)
    feedback.sort(
        key=lambda item: (
            item.get("allocation", {}).get("mean_flair_score") is None
            and item.get("direction", {}).get("mean_flair_score") is None,
            -(item.get("allocation", {}).get("mean_flair_score") or item.get("direction", {}).get("mean_flair_score") or 0.0),
            str(item[key]),
        )
    )
    return feedback


def _venue_matches(row: Mapping[str, Any], venue: str | None) -> bool:
    if venue is None:
        return True
    return str(row.get("venue") or "").strip().upper() == str(venue).strip().upper()


def _venue_allocation_block(
    rows: Sequence[Mapping[str, Any]],
    *,
    venue: str | None,
    min_n: int,
) -> dict[str, Any] | None:
    """Venue-grain allocation for the agent, plus a standing baseline control."""
    if venue is None:
        return None
    in_venue = [row for row in rows if _venue_matches(row, venue)]
    agent_alloc = [
        row
        for row in in_venue
        if _is_agent_selector(row) and _verdict_basis_of(row) == "allocation"
    ]
    block = _basis_block(agent_alloc, rate_key="beat_bench_rate", min_n=min_n)
    if block is None:
        return None
    baseline_alloc = [
        row
        for row in in_venue
        if not _is_agent_selector(row) and _verdict_basis_of(row) == "allocation"
    ]
    control = _basis_block(baseline_alloc, rate_key="beat_bench_rate", min_n=min_n)
    if control is not None:
        block["vs_baseline"] = {
            "n": control["n"],
            "beat_bench_rate": control["beat_bench_rate"],
            "lift": float(block["beat_bench_rate"]) - float(control["beat_bench_rate"]),
        }
    return block


def selection_feedback_digest(
    rows: Sequence[Mapping[str, Any]],
    *,
    venue: str | None = None,
    min_n: int = MIN_FEEDBACK_N,
) -> dict[str, Any]:
    """Compact market-as-judge digest for the universe agent. Never a hotlist."""

    scoped = [
        row
        for row in rows
        if _is_agent_selector(row) and _venue_matches(row, venue)
    ]
    families = _two_basis_feedback(scoped, "family", min_n=min_n)
    roles = _two_basis_feedback(scoped, "role", min_n=min_n)
    allocation = _venue_allocation_block(rows, venue=venue, min_n=min_n)
    n_evaluated = sum(1 for row in scoped if row.get("verdict") in {"gagnant", "perdant"})
    n_non_evaluable = sum(1 for row in scoped if row.get("verdict") == "non_evaluable")
    horizons = sorted(
        {int(row["horizon_sessions"]) for row in scoped if row.get("horizon_sessions") is not None}
    )
    payload: dict[str, Any] = {
        "role": SELECTION_FEEDBACK_ROLE,
        "status": "observed" if families or roles or allocation else "insufficient",
        "horizon_sessions": horizons[0] if len(horizons) == 1 else None,
        "min_n": min_n,
        "n_evaluated": n_evaluated,
        "n_non_evaluable": n_non_evaluable,
        "families": families,
        "roles": roles,
    }
    if allocation is not None:
        payload["allocation"] = allocation
    return payload


def compare_selection_selectors(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Agent vs deterministic baseline, split by verdict basis. Analytics only."""

    buckets: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        "agent": {"allocation": [], "direction": []},
        "baseline_fallback": {"allocation": [], "direction": []},
    }
    for row in rows:
        selector = str(row.get("selector") or "").strip() or "agent"
        if selector not in buckets:
            continue
        basis = _verdict_basis_of(row)
        if basis not in {"allocation", "direction"}:
            continue
        buckets[selector][basis].append(row)

    def _rates(items: Sequence[Mapping[str, Any]], *, rate_key: str) -> dict[str, Any]:
        n_gagnant = sum(1 for row in items if row.get("verdict") == "gagnant")
        n_perdant = sum(1 for row in items if row.get("verdict") == "perdant")
        decisive = n_gagnant + n_perdant
        scores = [float(row["flair_score"]) for row in items if row.get("flair_score") is not None]
        return {
            "n": decisive,
            rate_key: (n_gagnant / decisive) if decisive else None,
            "mean_flair_score": _mean(scores),
        }

    return {
        selector: {
            "allocation": _rates(by_basis["allocation"], rate_key="beat_bench_rate"),
            "direction": _rates(by_basis["direction"], rate_key="win_rate"),
        }
        for selector, by_basis in buckets.items()
    }


def iter_mandate_payloads(state_dir: str | Path):
    """Yield mandate snapshots without loading the full history into memory."""

    root = Path(state_dir)
    history = root / "universe_mandates" / "history.jsonl"
    if history.is_file():
        with history.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload
        return
    venues = root / "universe_mandates" / "active" / "venues"
    if not venues.is_dir():
        return
    for path in sorted(venues.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            yield payload


def load_mandate_selections(state_dir: str | Path) -> list[UniverseSelection]:
    return selections_from_mandate_payloads(iter_mandate_payloads(state_dir))


def _needed_verdict_bases(selection: UniverseSelection) -> tuple[str, ...]:
    bases = ["allocation"]
    if directional_action(selection.allowed_sides) is not None:
        bases.append("direction")
    return tuple(bases)


def refresh_selection_outcomes(
    state_dir: str | Path,
    data_source: DataSource,
    *,
    store_opener: SelectionOutcomeStoreOpener,
    scope_store_opener: CandidateScopeStoreOpener,
    horizon_sessions: int = DEFAULT_FORWARD_SESSIONS,
    limit: int = 128,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
) -> dict[str, Any]:
    """Score a bounded batch of still-unjudged mandate selections. Fail-open caller."""

    store = store_opener(state_dir)
    semantics_version = store.ensure_selection_semantics()
    scope_reader = scope_store_opener(state_dir)
    existing = {
        (
            str(row.get("mandate_id") or ""),
            str(row.get("symbol") or ""),
            str(row.get("as_of") or ""),
            int(row.get("horizon_sessions") or 0),
            str(row.get("verdict_basis") or "direction"),
        )
        for row in store.load_outcomes()
    }
    pending: list[UniverseSelection] = []
    for selection in load_mandate_selections(state_dir):
        missing = any(
            (
                selection.mandate_id,
                selection.symbol,
                selection.as_of,
                int(horizon_sessions),
                basis,
            )
            not in existing
            for basis in _needed_verdict_bases(selection)
        )
        if not missing:
            continue
        pending.append(selection)
        if len(pending) >= max(0, int(limit)):
            break
    evaluated = evaluate_selections(
        pending,
        data_source,
        horizon_sessions=horizon_sessions,
        scope_reader=scope_reader,
    )
    persist_and_score(store, evaluated, shrinkage_k=shrinkage_k)
    return {
        "pending": len(pending),
        "evaluated": len(evaluated),
        "stored": store.count(),
        "bench_unresolved": sum(1 for item in evaluated if item.scope_missing),
        "semantics_version": semantics_version,
    }


def summarize_outcomes(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """JSON-ready answer: which families / venues / roles pay or disappoint."""
    horizons = sorted({int(row["horizon_sessions"]) for row in rows if row.get("horizon_sessions") is not None})
    n_gagnant = sum(1 for row in rows if row.get("verdict") == "gagnant")
    n_perdant = sum(1 for row in rows if row.get("verdict") == "perdant")
    n_neutre = sum(1 for row in rows if row.get("verdict") == "neutre")
    n_non_evaluable = sum(1 for row in rows if row.get("verdict") == "non_evaluable")
    by_family = _group_summary(rows, "family")
    by_venue = _group_summary(rows, "venue")
    by_role = _group_summary(rows, "role")
    return {
        "n": len(rows),
        "n_gagnant": n_gagnant,
        "n_perdant": n_perdant,
        "n_neutre": n_neutre,
        "n_non_evaluable": n_non_evaluable,
        "horizon_sessions": horizons[0] if len(horizons) == 1 else None,
        "horizons": horizons,
        "by_family": by_family,
        "by_venue": by_venue,
        "by_role": by_role,
        "pays": {
            "families": _split_pays_decoit(by_family, "family")["pays"],
            "venues": _split_pays_decoit(by_venue, "venue")["pays"],
            "roles": _split_pays_decoit(by_role, "role")["pays"],
        },
        "decoit": {
            "families": _split_pays_decoit(by_family, "family")["decoit"],
            "venues": _split_pays_decoit(by_venue, "venue")["decoit"],
            "roles": _split_pays_decoit(by_role, "role")["decoit"],
        },
    }
