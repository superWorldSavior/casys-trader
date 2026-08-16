"""Pure projections for cockpit data health and configured sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trader.domain.learnings.scoring import citation_utility
from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.projections.universe import venue_of_safe
from trader.support.coercion import dict_list, finite_float

VENUE_DISPLAY: dict[str, str] = {"TW": "TPE", "EU": "EU", "US": "US"}
VENUE_ORDER: tuple[str, ...] = ("TW", "EU", "US")


@dataclass(frozen=True)
class StaleSymbolRow:
    symbol: str
    age_minutes: float | None


@dataclass(frozen=True)
class VenueFreshnessRow:
    venue: str
    display_name: str
    symbol_count: int
    is_stale: bool
    max_age_minutes: float | None
    stale_symbols: tuple[StaleSymbolRow, ...]


@dataclass(frozen=True)
class FreshnessProjection:
    venues: list[VenueFreshnessRow]
    stale_symbols: list[StaleSymbolRow]

    @property
    def has_stale(self) -> bool:
        return bool(self.stale_symbols)


@dataclass(frozen=True)
class SourceRow:
    name: str
    detail: str


@dataclass(frozen=True)
class SourcesProjection:
    rows: list[SourceRow]


@dataclass(frozen=True)
class FxRateRow:
    currency: str
    rate: float


@dataclass(frozen=True)
class FxRatesProjection:
    source_available: bool
    rows: list[FxRateRow]


@dataclass(frozen=True)
class LlmHealthProjection:
    calls_label: str
    fallbacks_label: str
    calls_this_cycle: int | None
    total_fills: int
    total_fallbacks: int


@dataclass(frozen=True)
class LearningRow:
    symbol: str
    note: str


@dataclass(frozen=True)
class LearningsProjection:
    pending_label: str
    consolidation_label: str
    last_run_label: str
    notes: list[LearningRow]


@dataclass(frozen=True)
class UniverseHealthProjection:
    total_symbols: int
    venue_counts: tuple[tuple[str, int], ...]
    symbols_label: str
    hot_total: int
    hotset_label: str


@dataclass(frozen=True)
class MemoryHealthProjection:
    available: bool
    missing_label: str
    notes_label: str
    lift_value: float | None
    lift_label: str
    useful_label: str
    rules_label: str
    n_helps: int
    n_hurts: int
    sync_label: str
    sync_is_error: bool
    situation_label: str


def symbols_by_venue(state: dict) -> dict[str, list[str]]:
    """Group universe symbols by venue while excluding FX pairs."""

    grouped: dict[str, list[str]] = {}
    for raw_symbol in state.get("universe_symbols") or []:
        symbol = str(raw_symbol)
        venue = venue_of_safe(symbol)
        if venue != "FX":
            grouped.setdefault(venue, []).append(symbol)
    return grouped


def project_freshness(state: dict) -> FreshnessProjection:
    """Project venue freshness and globally age-sorted stale symbols."""

    stale_data = f.safe_dict(state.get("stale_market_data"))
    grouped = symbols_by_venue(state)
    sorted_venues = [venue for venue in VENUE_ORDER if venue in grouped]
    sorted_venues += sorted(
        venue for venue in grouped if venue not in VENUE_ORDER
    )

    venue_rows: list[VenueFreshnessRow] = []
    all_stale: list[StaleSymbolRow] = []
    for venue in sorted_venues:
        symbols = grouped.get(venue, [])
        stale_symbols = tuple(
            StaleSymbolRow(
                symbol=symbol,
                age_minutes=f.staleness_age_m(state, symbol),
            )
            for symbol in symbols
            if symbol in stale_data
        )
        all_stale.extend(stale_symbols)
        venue_rows.append(
            VenueFreshnessRow(
                venue=venue,
                display_name=VENUE_DISPLAY.get(venue, venue),
                symbol_count=len(symbols),
                is_stale=bool(stale_symbols),
                max_age_minutes=max(
                    (
                        row.age_minutes
                        for row in stale_symbols
                        if row.age_minutes is not None
                    ),
                    default=None,
                ),
                stale_symbols=stale_symbols,
            )
        )

    all_stale.sort(
        key=lambda row: row.age_minutes or 0.0,
        reverse=True,
    )
    return FreshnessProjection(venues=venue_rows, stale_symbols=all_stale)


def project_sources(
    state: dict,
    *,
    ib_host: str,
    ib_port: str,
    ib_client_id: str,
) -> SourcesProjection:
    """Project configured source labels from explicit environment values."""

    rows = [
        SourceRow("IB gateway", f"{ib_host}:{ib_port} · id {ib_client_id}"),
        SourceRow("yfinance", "fallback"),
        SourceRow("news", "yahoo"),
    ]
    macro = (
        state.get("macro")
        or state.get("macro_data")
        or state.get("macro_calendar")
    )
    if macro:
        if isinstance(macro, dict):
            next_fomc = macro.get("next_fomc") or macro.get("fomc_next")
            detail = f"next FOMC {next_fomc}" if next_fomc else "available"
        else:
            detail = "available"
        rows.append(SourceRow("macro", detail))
    return SourcesProjection(rows=rows)


def project_fx_rates(state: dict) -> FxRatesProjection:
    """Project the supported non-USD FX rates in display order."""

    raw_rates = f.safe_dict(state.get("fx_rates"))
    rows: list[FxRateRow] = []
    for currency in ("EUR", "CHF", "TWD"):
        rate = finite_float(raw_rates.get(currency), default=None)
        if rate is not None:
            rows.append(FxRateRow(currency=currency, rate=rate))
    return FxRatesProjection(source_available=bool(raw_rates), rows=rows)


def project_llm_health(state: dict) -> LlmHealthProjection:
    """Project model-call and fallback counters into stable operator labels."""

    status = f.safe_dict(state.get("daemon_status"))
    kpis = f.safe_dict(state.get("kpis"))
    model_performance = dict_list(kpis.get("model_performance"))

    calls_value = finite_float(status.get("model_calls_used"), default=None)
    calls_this_cycle = int(calls_value) if calls_value is not None else None
    total_fills = sum(
        int(finite_float(row.get("fills"), default=0.0) or 0.0)
        for row in model_performance
    )
    total_fallbacks = sum(
        int(finite_float(row.get("fallbacks"), default=0.0) or 0.0)
        for row in model_performance
    )

    if calls_this_cycle is not None and total_fills:
        calls_label = f"{calls_this_cycle} this cycle · {total_fills} total"
    elif calls_this_cycle is not None:
        calls_label = f"{calls_this_cycle} this cycle"
    else:
        calls_label = "—"

    return LlmHealthProjection(
        calls_label=calls_label,
        fallbacks_label=f"{total_fallbacks} · on error → HOLD",
        calls_this_cycle=calls_this_cycle,
        total_fills=total_fills,
        total_fallbacks=total_fallbacks,
    )


def project_learnings(state: dict) -> LearningsProjection:
    """Project consolidation state and non-empty recent learning notes."""

    pending = state.get("learnings_pending_count")
    consolidation = f.safe_dict(state.get("consolidation_status"))
    if consolidation:
        phase = str(
            consolidation.get("phase")
            or consolidation.get("status")
            or "idle"
        )
        last_timestamp = consolidation.get("last_run_ts") or consolidation.get(
            "ts"
        )
        consolidation_label = f"consolidation {phase}"
        last_run_label = f.hhmm(last_timestamp) if last_timestamp else ""
    else:
        consolidation_label = "consolidation idle"
        last_run_label = ""

    notes = [
        LearningRow(
            symbol=str(row.get("symbol") or "").strip(),
            note=str(row.get("note") or "").strip(),
        )
        for row in dict_list(state.get("learnings"))
        if str(row.get("note") or "").strip()
    ]
    return LearningsProjection(
        pending_label=str(pending) if pending is not None else "?",
        consolidation_label=consolidation_label,
        last_run_label=last_run_label,
        notes=notes,
    )


def project_universe_health(state: dict) -> UniverseHealthProjection:
    """Project universe counts by venue and the current rotating hot-set size."""

    grouped = symbols_by_venue(state)
    ordered_venues = [venue for venue in VENUE_ORDER if venue in grouped]
    ordered_venues += sorted(
        venue for venue in grouped if venue not in VENUE_ORDER
    )
    venue_counts = tuple(
        (VENUE_DISPLAY.get(venue, venue), len(grouped[venue]))
        for venue in ordered_venues
        if grouped[venue]
    )
    total_symbols = sum(count for _, count in venue_counts)
    venue_label = " / ".join(
        f"{display_name} {count}" for display_name, count in venue_counts
    )
    symbols_label = (
        f"{total_symbols} · {venue_label}" if venue_label else str(total_symbols)
    )

    venue_state = f.safe_dict(state.get("venue_state"))
    venues_data = f.safe_dict(venue_state.get("venues"))
    hot_total = sum(
        len(venue_data.get("hotlist") or [])
        for venue_data in venues_data.values()
        if isinstance(venue_data, dict)
    )
    return UniverseHealthProjection(
        total_symbols=total_symbols,
        venue_counts=venue_counts,
        symbols_label=symbols_label,
        hot_total=hot_total,
        hotset_label=f"{hot_total} rotating" if hot_total else "—",
    )


def _count_rule_utilities(rules: list[dict]) -> tuple[int, int]:
    helps = 0
    hurts = 0
    for row in rules:
        utility = citation_utility(
            q_value=float(row.get("q_value") or 0.0),
            q_updates=int(row.get("q_updates") or 0),
        )
        if utility == "helps":
            helps += 1
        elif utility == "hurts":
            hurts += 1
    return helps, hurts


def project_memory(state: dict, *, now: datetime) -> MemoryHealthProjection:
    """Project the compact MEMORY health strip from the read-model snapshot."""

    snapshot = f.safe_dict(state.get("memory_health"))
    store_available = bool(snapshot.get("store_available"))
    n_notes = snapshot.get("n_notes")
    lift = finite_float(snapshot.get("lift"), default=None)
    useful = finite_float(snapshot.get("useful_rate"), default=None)
    base = finite_float(snapshot.get("base_rate"), default=None)
    rules = [
        row for row in dict_list(snapshot.get("active_rules")) if isinstance(row, dict)
    ]
    n_helps, n_hurts = _count_rule_utilities(rules) if store_available else (0, 0)

    if lift is None:
        lift_label = "—"
    else:
        lift_label = f"{lift * 100.0:+.1f} pp"

    if useful is None and base is None:
        useful_label = "—"
    elif useful is None:
        useful_label = f"base {base:.0%}" if base is not None else "—"
    elif base is None:
        useful_label = f"recall {useful:.0%}"
    else:
        useful_label = f"recall {useful:.0%} · base {base:.0%}"

    notes_label = "—" if n_notes is None else str(int(n_notes))
    rules_label = f"{n_helps} helps · {n_hurts} hurts" if store_available else "—"

    as_of = snapshot.get("sync_as_of")
    parsed = f.parse_ts(as_of)
    status = str(snapshot.get("sync_status") or "").strip()
    if not snapshot.get("sync_available"):
        sync_label = "no sync yet"
    elif parsed is None:
        sync_label = status or "—"
    else:
        clock = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        age_minutes = (clock - parsed).total_seconds() / 60.0
        age_label = f.age_m(age_minutes)
        sync_label = f"{age_label} ago" + (f" · {status}" if status else "")

    sit_n = snapshot.get("situation_n")
    sit_eval = snapshot.get("situation_n_evaluated")
    if sit_n is None:
        situation_label = "—"
    else:
        situation_label = f"{int(sit_eval or 0)}/{int(sit_n)}"
    situation_error = str(snapshot.get("situation_error") or "").strip()

    return MemoryHealthProjection(
        available=store_available,
        missing_label="memory store unavailable",
        notes_label=notes_label,
        lift_value=lift,
        lift_label=lift_label,
        useful_label=useful_label,
        rules_label=rules_label,
        n_helps=n_helps,
        n_hurts=n_hurts,
        sync_label=sync_label,
        sync_is_error=status == "error" or bool(situation_error),
        situation_label=situation_label,
    )


__all__ = [
    "FreshnessProjection",
    "FxRateRow",
    "FxRatesProjection",
    "LearningRow",
    "LearningsProjection",
    "LlmHealthProjection",
    "MemoryHealthProjection",
    "SourceRow",
    "SourcesProjection",
    "StaleSymbolRow",
    "UniverseHealthProjection",
    "VENUE_DISPLAY",
    "VENUE_ORDER",
    "VenueFreshnessRow",
    "project_freshness",
    "project_fx_rates",
    "project_learnings",
    "project_llm_health",
    "project_memory",
    "project_sources",
    "project_universe_health",
    "symbols_by_venue",
]
