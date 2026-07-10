"""Pure symbol-row projection for the cockpit universe page."""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from trader.domain.universe.user_overrides import UserOverrides
from trader.interfaces.cockpit import format as f
from trader.reporting.read_models.runtime_state import (
    _safe_float,
    _safe_list_of_dicts,
)


VENUE_ORDER = ("TW", "EU", "US")


def venue_of_safe(symbol: str) -> str:
    """Resolve a symbol venue without allowing malformed input to break the UI."""

    try:
        from trader.market.rotation.wiring import venue_of

        return venue_of(symbol)
    except Exception:
        return "?"


class SymbolRowData(NamedTuple):
    """Presentation-ready data for one universe table row."""

    symbol: str
    venue: str
    name: str
    state_label: str
    pos: str
    last_decision: str
    last_decision_action: str
    wake_text: str
    wake_urgent: bool
    data_text: str
    data_stale: bool


def build_symbol_rows(
    state: dict,
    overrides: UserOverrides,
    *,
    now: datetime,
    name_col_width: int = 20,
) -> list[SymbolRowData]:
    """Project tolerant runtime state into ordered, presentation-ready rows."""

    universe_symbols = list(state.get("universe_symbols") or [])
    venue_state_raw = f.safe_dict(state.get("venue_state"))
    venues_raw = f.safe_dict(venue_state_raw.get("venues"))
    company_map = f.safe_dict(state.get("company_map"))
    current_symbol = str(
        f.safe_dict(state.get("daemon_status")).get("current_symbol") or ""
    )
    recent = _safe_list_of_dicts(state.get("recent_decisions"))
    symbol_wakes = f.safe_dict(state.get("symbol_wakes"))
    stale_data = f.safe_dict(state.get("stale_market_data"))

    holdings_by_symbol = {
        f.holding_symbol(holding): holding
        for holding in _safe_list_of_dicts(
            f.safe_dict(state.get("portfolio")).get("holdings")
        )
    }

    last_decision_by_symbol: dict[str, dict] = {}
    for row in reversed(recent):
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in last_decision_by_symbol:
            last_decision_by_symbol[symbol] = row

    first_watch_expiry_by_symbol: dict[str, datetime] = {}
    for watch in _safe_list_of_dicts(state.get("indicator_watches")):
        symbol = str(watch.get("symbol") or "")
        expires_at = f.parse_ts(watch.get("expires_at"))
        if symbol and expires_at and (
            symbol not in first_watch_expiry_by_symbol
            or expires_at < first_watch_expiry_by_symbol[symbol]
        ):
            first_watch_expiry_by_symbol[symbol] = expires_at

    symbols_by_venue: dict[str, list[str]] = {venue: [] for venue in VENUE_ORDER}
    for symbol in universe_symbols:
        symbols_by_venue.setdefault(venue_of_safe(symbol), []).append(symbol)

    rows: list[SymbolRowData] = []
    for venue in VENUE_ORDER:
        symbols = symbols_by_venue.get(venue, [])
        if not symbols:
            continue
        venue_info = f.safe_dict(venues_raw.get(venue))
        hotlist = set(venue_info.get("hotlist") or [])
        scores = f.safe_dict(venue_info.get("scores"))

        def sort_key(
            symbol: str,
            hot_symbols: set = hotlist,
            symbol_scores: dict = scores,
        ) -> tuple:
            return (
                0 if symbol in hot_symbols else 1,
                -(_safe_float(symbol_scores.get(symbol), default=0.0) or 0.0),
            )

        for symbol in sorted(symbols, key=sort_key):
            override_status = overrides.status_of(symbol)
            name = str(company_map.get(symbol) or "")
            if len(name) > name_col_width:
                name = f"{name[: name_col_width - 1]}…"

            if override_status == "pinned":
                state_label = "pinned"
            elif override_status == "banned":
                state_label = "banned"
            elif symbol in hotlist:
                state_label = "hot"
            else:
                state_label = "pool"

            holding = holdings_by_symbol.get(symbol)
            if holding:
                quantity = f.holding_quantity(holding)
                pos = "L" if quantity > 0 else ("S" if quantity < 0 else "—")
            else:
                pos = "—"

            if symbol == current_symbol:
                decision_text, decision_action = "deciding now ▸", "deciding"
            elif override_status == "banned":
                decision_text, decision_action = "excluded", "excluded"
            else:
                decision = last_decision_by_symbol.get(symbol)
                if decision:
                    action = str(decision.get("action") or "").upper()
                    timestamp = f.hhmm(
                        decision.get("cycle_ts") or decision.get("ts")
                    )
                    confidence = _safe_float(
                        decision.get("confidence"), default=None
                    )
                    confidence_text = (
                        f" .{int(confidence * 100):02d}"
                        if confidence is not None
                        else ""
                    )
                    decision_text = f"{timestamp} {action}{confidence_text}"
                    decision_action = action
                else:
                    decision_text, decision_action = "—", "—"

            wake_raw = symbol_wakes.get(symbol)
            wake_at = (
                f.parse_ts(wake_raw)
                if wake_raw
                else first_watch_expiry_by_symbol.get(symbol)
            )
            if wake_at and wake_at > now:
                wake_text = f.countdown(wake_at, now=now)
                wake_urgent = (wake_at - now).total_seconds() < 7200
            else:
                wake_text, wake_urgent = "—", False

            stale_entry = f.safe_dict(stale_data.get(symbol))
            if stale_entry:
                age_minutes = _safe_float(
                    stale_entry.get("data_age_minutes"), default=None
                )
                data_text = (
                    f"▲ {f.age_m(age_minutes)}"
                    if age_minutes is not None
                    else "▲ ?"
                )
                data_stale = True
            else:
                data_text, data_stale = "● fresh", False

            rows.append(
                SymbolRowData(
                    symbol=symbol,
                    venue=venue,
                    name=name,
                    state_label=state_label,
                    pos=pos,
                    last_decision=decision_text,
                    last_decision_action=decision_action,
                    wake_text=wake_text,
                    wake_urgent=wake_urgent,
                    data_text=data_text,
                    data_stale=data_stale,
                )
            )
    return rows


__all__ = ["SymbolRowData", "VENUE_ORDER", "build_symbol_rows", "venue_of_safe"]
