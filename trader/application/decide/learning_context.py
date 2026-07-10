"""Automatic, bounded FLAIR context for one decision symbol."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from trader.agent.protocol.types import Decision
from trader.domain.semantic.catalog import family_for_symbol


AUTO_RECALL_LIMIT = 2


def build_auto_learning_recall(
    provider: Callable[[dict], dict] | None,
    *,
    symbol: str,
    limit: int = AUTO_RECALL_LIMIT,
) -> dict | None:
    """Return symbol-first, then family fallback experiences without embedding calls."""

    if provider is None or limit <= 0:
        return None
    selected: list[dict] = []
    seen: set[int] = set()

    def add_rows(payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        for row in payload.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            note_id = row.get("id")
            if not isinstance(note_id, int) or note_id in seen:
                continue
            seen.add(note_id)
            selected.append(dict(row))
            if len(selected) >= limit:
                return

    try:
        add_rows(provider({"symbol": symbol, "limit": limit}))
        family = family_for_symbol(symbol)
        if len(selected) < limit and family:
            add_rows(provider({"family": family, "limit": limit - len(selected)}))
    except Exception:  # noqa: BLE001 - recall is optional and never blocks a decision
        return None
    if not selected:
        return None
    return {
        "scope": "automatic_symbol_then_family",
        "symbol": symbol,
        "family": family_for_symbol(symbol),
        "rows": selected[:limit],
        "details_via_tool": "recall_learnings",
    }


def attach_auto_recall_trace(decision: Decision, recall: dict | None) -> Decision:
    """Attach automatic note ids without pretending the model called a tool."""

    if not isinstance(recall, Mapping):
        return decision
    note_ids = [
        row.get("id")
        for row in recall.get("rows") or []
        if isinstance(row, Mapping) and isinstance(row.get("id"), int)
    ]
    if not note_ids:
        return decision
    current = dict(decision.domain_tools) if isinstance(decision.domain_tools, dict) else {}
    return replace(
        decision,
        domain_tools={
            **current,
            "automatic_recall": {
                "note_ids": note_ids,
                "mode": "automatic_push",
                "symbol": decision.symbol,
                "family": recall.get("family"),
            },
        },
    )


__all__ = [
    "AUTO_RECALL_LIMIT",
    "attach_auto_recall_trace",
    "build_auto_learning_recall",
]
