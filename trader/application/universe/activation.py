"""Validation policy at prepared-universe activation time."""

from __future__ import annotations

from typing import Any


def validate_prepared_hotlist(
    raw: Any,
    *,
    pool: set[str],
    sticky: set[str],
    cap: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Revalidate a full agent selection against its current activation scope."""

    if not isinstance(raw, list):
        return [], [{"reason": "selected_hotlist_not_list"}]
    selected: list[str] = []
    rejects: list[dict[str, Any]] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            rejects.append({"symbol": value, "reason": "invalid_symbol"})
            continue
        symbol = value.strip()
        if symbol in selected:
            rejects.append({"symbol": symbol, "reason": "duplicate"})
        elif symbol not in pool:
            rejects.append({"symbol": symbol, "reason": "out_of_pool"})
        elif symbol in sticky:
            rejects.append({"symbol": symbol, "reason": "sticky_outside_quota"})
        elif len(selected) >= cap:
            rejects.append({"symbol": symbol, "reason": "cap_exceeded"})
        else:
            selected.append(symbol)
    return selected, rejects
