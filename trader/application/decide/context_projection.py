"""Projection focalisee du contexte de decision grain-symbole.

Le daemon calcule un contexte riche une fois par cycle. En mode queue, chaque
tache ne decide pourtant qu'un symbole : recopier le dump global complet dans
chaque prompt dilue le signal et duplique inutilement risque, plans et historique.

Cette projection conserve :

* les faits globaux qui influencent vraiment une decision (portefeuille, regime,
  limites et horloges) ;
* un radar cross-asset borne (symbole cible, pairs, positions et anomalies) ;
* le detail complet du symbole cible et de ses pairs les plus informatifs ;
* les resumes globaux, avec le detail disponible via les domain tools.

Les faits produits par Univers et les analystes micro/news vivent dans le payload
par symbole et ne passent donc pas par cette projection.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import math
from typing import Any


CONTEXT_SCOPE_VERSION = "decision_focus_v1"
MAX_FOCUS_ROWS = 8
MAX_RADAR_ROWS = 32

RADAR_COLUMNS = (
    "s",
    "f",
    "p",
    "r",
    "z",
    "rs",
    "sz",
    "r_d",
    "z_d",
    "rs_d",
    "sz_d",
    "reg",
    "vs",
    "htf",
    "aligned",
    "sig",
)

_PEER_SCORE_COLUMNS = ("r", "z", "rs", "sz", "r_d", "z_d", "rs_d", "sz_d")

_ATTRIBUTION_SUMMARY_KEYS = (
    "n_closed_trades",
    "realized_pnl",
    "realized_gross_pnl",
    "total_commissions",
    "win_rate",
    "avg_pnl",
    "avg_holding_minutes",
    "regime",
)


def project_shared_context_for_symbol(shared_context: dict, *, symbol: str) -> dict:
    """Return a prompt-safe view of one cycle for one decision symbol.

    Empty/minimal contexts are left untouched so tests and degraded paths do not
    acquire synthetic facts. Rich production contexts receive explicit scope
    metadata and bounded nested projections. The input is never mutated.
    """

    if not shared_context:
        return {}

    projected = dict(shared_context)
    projected["risk_capacity"] = _project_risk_capacity(shared_context.get("risk_capacity"), symbol)
    projected["active_plans_summary"] = _project_active_plans(
        shared_context.get("active_plans_summary"), symbol
    )
    projected["attribution"] = _project_attribution(shared_context.get("attribution"), symbol)
    projected["stale_market_data"] = _project_stale_market_data(
        shared_context.get("stale_market_data"), symbol
    )
    projected["kpis"] = _project_kpis(shared_context.get("kpis"))
    projected["learnings"] = _project_learnings(shared_context.get("learnings"))
    projected["cockpit"] = _project_cockpit(
        shared_context.get("cockpit"),
        symbol=symbol,
        portfolio=shared_context.get("portfolio"),
    )

    return {
        "decision_context_scope": {
            "version": CONTEXT_SCOPE_VERSION,
            "symbol": symbol,
            "push": "target_research+risk+portfolio+bounded_cross_asset_radar",
            "pull": [
                "get_indicator_context",
                "get_active_plans",
                "get_attribution",
                "recall_learnings",
            ],
        },
        **projected,
    }


def _project_risk_capacity(raw: object, symbol: str) -> object:
    if not isinstance(raw, Mapping):
        return raw
    projected = dict(raw)
    per_symbol = raw.get("per_symbol")
    if isinstance(per_symbol, Mapping):
        target = per_symbol.get(symbol)
        projected["per_symbol"] = {symbol: target} if isinstance(target, Mapping) else {}
        projected["scope"] = "target_only"
    return projected


def _project_active_plans(raw: object, symbol: str) -> object:
    if not isinstance(raw, list):
        return raw
    rows = [dict(row) for row in raw if isinstance(row, Mapping)]
    by_kind = Counter(str(row.get("kind") or "unknown") for row in rows)
    symbols = {str(row.get("symbol")) for row in rows if row.get("symbol")}
    return {
        "scope": "target_plus_global_counts",
        "total": len(rows),
        "symbol_count": len(symbols),
        "by_kind": dict(sorted(by_kind.items())),
        "target": [row for row in rows if row.get("symbol") == symbol],
        "details_via_tool": "get_active_plans",
    }


def _project_attribution(raw: object, symbol: str) -> object:
    if not isinstance(raw, Mapping):
        return raw
    projected = {key: raw[key] for key in _ATTRIBUTION_SUMMARY_KEYS if key in raw}
    recent = raw.get("recent_trips")
    if isinstance(recent, list):
        projected["recent_trips"] = [
            dict(row)
            for row in recent
            if isinstance(row, Mapping) and row.get("symbol") == symbol
        ]
    by_symbol = raw.get("by_symbol")
    if isinstance(by_symbol, list):
        projected["by_symbol"] = [
            dict(row)
            for row in by_symbol
            if isinstance(row, Mapping) and row.get("symbol") == symbol
        ]
    projected["scope"] = {
        "recent_trips": "target_only",
        "breakdowns": "pull_only",
        "full_via_tool": "get_attribution",
    }
    return projected


def _project_stale_market_data(raw: object, symbol: str) -> object:
    if not isinstance(raw, Mapping):
        return raw
    target = raw.get(symbol)
    return {symbol: dict(target)} if isinstance(target, Mapping) else {}


def project_symbol_facts_for_prompt(facts: dict, *, symbol: str) -> dict:
    """Remove cross-universe mandate detail that does not concern ``symbol``.

    The full facts stay available to local domain tools. Only the model-facing
    copy is narrowed, and the caller's payload is never mutated.
    """

    if not facts:
        return {}
    projected = dict(facts)
    raw_mandate = facts.get("universe_mandate")
    if not isinstance(raw_mandate, Mapping):
        return projected

    mandate = dict(raw_mandate)
    symbol_mandate = raw_mandate.get("symbol_mandate")
    if not isinstance(symbol_mandate, Mapping):
        projected["universe_mandate"] = mandate
        return projected
    mandate_symbol = str(symbol_mandate.get("symbol") or symbol)
    if mandate_symbol != symbol:
        projected["universe_mandate"] = mandate
        return projected

    family_context = symbol_mandate.get("family_context")
    family = (
        str(family_context.get("family") or "")
        if isinstance(family_context, Mapping)
        else ""
    )
    family_postures = raw_mandate.get("family_postures")
    if family and isinstance(family_postures, Mapping):
        target_posture = family_postures.get(family)
        mandate["family_postures"] = (
            {family: dict(target_posture)}
            if isinstance(target_posture, Mapping)
            else ({family: target_posture} if target_posture is not None else {})
        )
    projected["universe_mandate"] = mandate
    return projected


def _project_kpis(raw: object) -> object:
    if not isinstance(raw, Mapping):
        return raw
    projected = dict(raw)
    positions = projected.pop("positions", None)
    if isinstance(positions, list):
        projected["positions_count"] = len(positions)

    model_rows = raw.get("model_performance")
    if isinstance(model_rows, list):
        compact_rows: list[dict[str, Any]] = []
        for candidate in model_rows:
            if not isinstance(candidate, Mapping):
                continue
            row = dict(candidate)
            symbols = row.pop("symbols", None)
            if isinstance(symbols, (list, tuple, set, frozenset)):
                row["symbol_count"] = len(symbols)
            compact_rows.append(row)
        projected["model_performance"] = compact_rows
    return projected


def _project_learnings(raw: object) -> object:
    if not isinstance(raw, Mapping):
        return raw
    projected = dict(raw)
    # Les anciens slots par symbole sont une mémoire épisodique poussée sans
    # retrieval. La situation micro de référence vient du mandat Univers ; seul un
    # company_intelligence_delta plus frais est poussé. L'expérience ciblée reste
    # accessible via FLAIR.
    projected.pop("by_symbol", None)
    projected["scope"] = {
        "experience": "pull_only_outcome_weighted",
        "full_via_tool": "recall_learnings",
    }
    return projected


def _project_cockpit(raw: object, *, symbol: str, portfolio: object) -> object:
    if not isinstance(raw, Mapping):
        return raw
    raw_columns = raw.get("cols")
    raw_rows = raw.get("rows")
    if not isinstance(raw_columns, list) or not isinstance(raw_rows, list):
        return dict(raw)
    columns = [str(column) for column in raw_columns]
    if "s" not in columns or "f" not in columns:
        return dict(raw)

    rows = [
        list(row)
        for row in raw_rows
        if isinstance(row, Sequence)
        and not isinstance(row, (str, bytes))
        and len(row) >= len(columns)
    ]
    indexes = {column: index for index, column in enumerate(columns)}
    symbol_index = indexes["s"]
    family_index = indexes["f"]
    row_by_symbol: dict[str, list] = {}
    for row in rows:
        row_symbol = str(row[symbol_index] or "")
        if row_symbol and row_symbol not in row_by_symbol:
            row_by_symbol[row_symbol] = row

    target = row_by_symbol.get(symbol)
    target_family = None if target is None else target[family_index]
    peers = []
    if target is not None:
        peer_candidates = [
            row
            for row in rows
            if row is not target and row[family_index] == target_family
        ]
        peers = sorted(
            peer_candidates,
            key=lambda row: (-_peer_score(row, indexes), str(row[symbol_index])),
        )[: MAX_FOCUS_ROWS - 1]

    focus_rows = ([target] if target is not None else []) + peers
    priority_symbols = [symbol]
    priority_symbols.extend(str(row[symbol_index]) for row in peers)
    priority_symbols.extend(_portfolio_symbols(portfolio))
    priority_symbols.extend(_highlight_symbols(raw.get("highlights")))
    priority_symbols.extend(
        str(row[symbol_index])
        for row in sorted(
            rows,
            key=lambda row: (-_peer_score(row, indexes), str(row[symbol_index])),
        )
    )
    radar_symbols = _unique_existing(priority_symbols, row_by_symbol, limit=MAX_RADAR_ROWS)
    radar_columns = [column for column in RADAR_COLUMNS if column in indexes]
    radar_indexes = [indexes[column] for column in radar_columns]
    radar_rows = [
        [row_by_symbol[row_symbol][index] for index in radar_indexes]
        for row_symbol in radar_symbols
    ]

    result = {
        "v": raw.get("v"),
        "window": raw.get("window"),
        "daily_window": raw.get("daily_window"),
        "scope": {
            "mode": CONTEXT_SCOPE_VERSION,
            "target": symbol,
            "family": target_family,
            "source_universe_rows": len(rows),
            "radar_rows": len(radar_rows),
            "focus_rows": len(focus_rows),
            "radar_policy": "target+family_peers+portfolio_holdings+global_highlights",
            "details_via_tool": "get_indicator_context",
        },
        "schema": "rows use cols; focus.rows use focus.cols and focus.schema",
        "cols": radar_columns,
        "rows": radar_rows,
        "focus": {
            "schema": raw.get("schema"),
            "cols": columns,
            "rows": focus_rows,
        },
        "highlights": raw.get("highlights"),
    }
    if "fee_ref_notional" in raw:
        result["fee_ref_notional"] = raw.get("fee_ref_notional")
    return result


def _peer_score(row: list, indexes: Mapping[str, int]) -> float:
    values = [_finite_abs(row[indexes[name]]) for name in _PEER_SCORE_COLUMNS if name in indexes]
    score = max(values, default=0.0)
    signal_index = indexes.get("sig")
    if signal_index is not None and row[signal_index]:
        score += 1.0
    return score


def _finite_abs(value: object) -> float:
    try:
        selected = abs(float(value))
    except (TypeError, ValueError):
        return 0.0
    return selected if math.isfinite(selected) else 0.0


def _portfolio_symbols(raw: object) -> list[str]:
    if not isinstance(raw, Mapping):
        return []
    holdings = raw.get("holdings")
    if not isinstance(holdings, list):
        return []
    return [
        str(row.get("symbol"))
        for row in holdings
        if isinstance(row, Mapping) and row.get("symbol")
    ]


def _highlight_symbols(raw: object) -> list[str]:
    if not isinstance(raw, Mapping):
        return []
    symbols: list[str] = []
    for candidates in raw.values():
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if isinstance(candidate, Mapping) and candidate.get("symbol"):
                symbols.append(str(candidate["symbol"]))
            elif (
                isinstance(candidate, Sequence)
                and not isinstance(candidate, (str, bytes))
                and candidate
            ):
                symbols.append(str(candidate[0]))
    return symbols


def _unique_existing(
    candidates: Sequence[str],
    row_by_symbol: Mapping[str, list],
    *,
    limit: int,
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for raw_symbol in candidates:
        symbol = str(raw_symbol or "")
        if not symbol or symbol in seen or symbol not in row_by_symbol:
            continue
        seen.add(symbol)
        selected.append(symbol)
        if len(selected) >= limit:
            break
    return selected


__all__ = [
    "CONTEXT_SCOPE_VERSION",
    "MAX_FOCUS_ROWS",
    "MAX_RADAR_ROWS",
    "RADAR_COLUMNS",
    "project_shared_context_for_symbol",
    "project_symbol_facts_for_prompt",
]
