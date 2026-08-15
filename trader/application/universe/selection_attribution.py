"""Evaluate universe selections via the market DataSource port and score them."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from trader.domain.market_data import MarketError
from trader.domain.universe.intelligence import UNCLASSIFIED_FAMILY
from trader.domain.universe.selection_attribution import (
    DEFAULT_FORWARD_SESSIONS,
    DEFAULT_SHRINKAGE_K,
    classify_selection_quality,
    directional_action,
    forward_return_over_sessions,
    score_selection_outcomes,
)
from trader.market.protocols import DataSource

DEFAULT_BAR_INTERVAL = "1d"
DEFAULT_BAR_LOOKBACK = "1y"

__all__ = [
    "DEFAULT_BAR_INTERVAL",
    "DEFAULT_BAR_LOOKBACK",
    "EvaluatedSelection",
    "SelectionOutcomeStore",
    "UniverseSelection",
    "evaluate_selection",
    "evaluate_selections",
    "persist_and_score",
    "selections_from_mandate_payload",
    "summarize_outcomes",
]


@dataclass(frozen=True)
class UniverseSelection:
    mandate_id: str
    symbol: str
    role: str
    allowed_sides: tuple[str, ...]
    as_of: str
    venue: str = ""
    family: str = UNCLASSIFIED_FAMILY


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


class SelectionOutcomeStore(Protocol):
    def upsert_outcomes(self, rows: Sequence[Mapping[str, Any]]) -> None: ...

    def load_outcomes(self) -> list[Mapping[str, Any]]: ...

    def update_flair_scores(self, scores: Mapping[int, float]) -> None: ...


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


def _selection(
    *,
    mandate_id: str,
    symbol: str,
    symbol_mandate: Mapping[str, Any],
    as_of: str,
    venue: str,
) -> UniverseSelection | None:
    if not mandate_id or not symbol or not as_of:
        return None
    return UniverseSelection(
        mandate_id=mandate_id,
        symbol=symbol,
        role=str(symbol_mandate.get("role") or "").strip(),
        allowed_sides=_allowed_sides(symbol_mandate.get("allowed_sides")),
        as_of=as_of,
        venue=venue,
        family=_family_of(symbol_mandate),
    )


def selections_from_mandate_payload(payload: Mapping[str, Any]) -> list[UniverseSelection]:
    """Project one mandate snapshot (history line or active slice) into selections."""
    symbols = payload.get("symbols")
    if isinstance(symbols, Mapping):
        mandate_id = str(payload.get("mandate_id") or "")
        as_of = str(payload.get("as_of") or "")
        venue = str(payload.get("venue") or "")
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
            )
            if item is not None:
                out.append(item)
        return out

    ref = payload.get("mandate_ref") if isinstance(payload.get("mandate_ref"), Mapping) else {}
    symbol_mandate = payload.get("symbol_mandate")
    if not isinstance(symbol_mandate, Mapping):
        return []
    item = _selection(
        mandate_id=str(ref.get("mandate_id") or ""),
        symbol=str(symbol_mandate.get("symbol") or "").strip(),
        symbol_mandate=symbol_mandate,
        as_of=str(ref.get("as_of") or ""),
        venue=str(ref.get("venue") or ""),
    )
    return [item] if item is not None else []


def evaluate_selection(
    selection: UniverseSelection,
    bars: Sequence[Any],
    *,
    horizon_sessions: int = DEFAULT_FORWARD_SESSIONS,
) -> EvaluatedSelection | None:
    """Judge one selection. Directional picks without a complete horizon are skipped."""
    forward = forward_return_over_sessions(bars, selection.as_of, horizon_sessions)
    if directional_action(selection.allowed_sides) is not None and forward is None:
        return None
    return EvaluatedSelection(
        mandate_id=selection.mandate_id,
        symbol=selection.symbol,
        role=selection.role,
        allowed_sides=selection.allowed_sides,
        as_of=selection.as_of,
        venue=selection.venue,
        family=selection.family,
        horizon_sessions=horizon_sessions,
        forward_return=forward,
        verdict=classify_selection_quality(selection.allowed_sides, forward),
    )


def evaluate_selections(
    selections: Iterable[UniverseSelection],
    data_source: DataSource,
    *,
    horizon_sessions: int = DEFAULT_FORWARD_SESSIONS,
    lookback: str = DEFAULT_BAR_LOOKBACK,
    interval: str = DEFAULT_BAR_INTERVAL,
) -> list[EvaluatedSelection]:
    """Fetch bars once per symbol through ``DataSource`` and evaluate each selection."""
    materialized = list(selections)
    bars_by_symbol: dict[str, list[Any]] = {}
    for symbol in dict.fromkeys(item.symbol for item in materialized):
        try:
            bars_by_symbol[symbol] = list(data_source.get_bars(symbol, lookback, interval))
        except MarketError:
            bars_by_symbol[symbol] = []
    evaluated: list[EvaluatedSelection] = []
    for selection in materialized:
        item = evaluate_selection(
            selection,
            bars_by_symbol.get(selection.symbol, ()),
            horizon_sessions=horizon_sessions,
        )
        if item is not None:
            evaluated.append(item)
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
    result = score_selection_outcomes(rows, shrinkage_k=shrinkage_k)
    store.update_flair_scores(result.get("scores") or {})
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


def _split_pays_decoit(groups: Sequence[Mapping[str, Any]], key: str) -> dict[str, list[str]]:
    pays: list[str] = []
    decoit: list[str] = []
    for item in groups:
        name = str(item.get(key) or "")
        if not name:
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
