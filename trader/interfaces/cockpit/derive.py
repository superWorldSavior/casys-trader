"""derive — dérivés d'état PURS pour le shell et les pages du cockpit casys.

Chaque fonction : (state dict [, now]) → dataclass/valeurs simples, sans I/O,
sans Textual, sans horloge implicite (``now`` injecté partout). Testable seul.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from trader.domain.execution.fill_accounting import POSITION_EPSILON
from trader.domain.planning.watches import is_armed_plan as _is_armed_plan
from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.aggregates import attention_items, open_venue_set
from trader.support.coercion import (
    dict_list as _safe_list_of_dicts,
    finite_float as _safe_float,
)


# ---------------------------------------------------------------------------
# Équité / retours
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquitySnapshot:
    equity: float = 0.0
    cash: float = 0.0
    cash_available: float = 0.0
    cash_ledger: float = 0.0
    cash_pct: float = 0.0
    return_pct: float = 0.0  # en points de %
    pnl_usd: float = 0.0
    unrealized: float = 0.0


def _cash_available_from_holdings(portfolio: dict, kpis: dict) -> tuple[float, float]:
    cash_ledger = _safe_float(
        portfolio.get("cash_ledger") or portfolio.get("cash") or kpis.get("cash"),
        default=0.0,
    ) or 0.0
    explicit_available = _safe_float(portfolio.get("cash_available"), default=None)
    if explicit_available is not None:
        return explicit_available, cash_ledger

    short_exposure = 0.0
    for holding in _safe_list_of_dicts(portfolio.get("holdings")):
        if f.holding_quantity(holding) < 0:
            short_exposure += f.holding_notional(holding)
    return cash_ledger - short_exposure, cash_ledger


def equity_snapshot(state: dict) -> EquitySnapshot:
    portfolio = f.safe_dict(state.get("portfolio"))
    kpis = f.safe_dict(state.get("kpis"))
    equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
    cash_available, cash_ledger = _cash_available_from_holdings(portfolio, kpis)
    starting = _safe_float(state.get("starting_cash"), default=cash_ledger) or cash_ledger
    return_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if return_pct is None:
        return_pct = (_safe_float(kpis.get("total_return"), default=0.0) or 0.0) * 100.0
    holdings = _safe_list_of_dicts(portfolio.get("holdings"))
    return EquitySnapshot(
        equity=equity,
        cash=cash_available,
        cash_available=cash_available,
        cash_ledger=cash_ledger,
        cash_pct=(cash_available / equity * 100.0) if equity else 0.0,
        return_pct=return_pct,
        pnl_usd=equity - starting,
        unrealized=sum(f.holding_pnl(h) for h in holdings),
    )


# ---------------------------------------------------------------------------
# Cycle / workers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CycleProgress:
    running: bool = False
    done: int = 0
    total: int = 0
    current_symbol: str | None = None


def cycle_progress(state: dict) -> CycleProgress:
    status = f.safe_dict(state.get("daemon_status"))
    done = status.get("decisions_done")
    total = status.get("symbols_total")
    phase = str(status.get("phase") or "")
    running = (
        phase != "cycle_completed"
        and isinstance(total, int)
        and total > 0
        and isinstance(done, int)
        and done < total
    )
    return CycleProgress(
        running=running,
        done=int(done) if isinstance(done, int) else 0,
        total=int(total) if isinstance(total, int) else 0,
        current_symbol=status.get("current_symbol") or None,
    )


def workers_activity_label(state: dict) -> str:
    activity = f.safe_dict(state.get("queue_worker_activity"))
    active = activity.get("active_workers")
    if active is None:
        return "—"
    try:
        active_int = int(active)
    except (TypeError, ValueError):
        return "—"
    return f"{active_int} active"


# ---------------------------------------------------------------------------
# Journal (home) — décisions racontables
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalEntry:
    time: str
    symbol: str
    action: str  # BUY | SELL | HOLD
    confidence: float | None
    rationale: str
    effect: str  # texte de la ligne effet ("watch: … · expires 3h58" / "filled …")
    effect_kind: str  # watch | fill | plan | wake | risk | none


def journal_entries(state: dict, *, now: datetime, limit: int = 8) -> list[JournalEntry]:
    """Dernières décisions, rationale d'abord — l'ordre reste chronologique desc."""
    decisions = _safe_list_of_dicts(state.get("decisions"))
    recent = _safe_list_of_dicts(state.get("recent_decisions"))
    seen: set[tuple[str, str, str, str, str]] = set()
    merged: list[dict] = []
    for row in [*decisions, *reversed(recent)]:
        key = (
            str(row.get("cycle_ts") or row.get("ts") or ""),
            str(row.get("sequence") or ""),
            str(row.get("symbol") or ""),
            str(row.get("action") or ""),
            str(row.get("reason") or row.get("rationale") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(
        key=lambda r: f.parse_ts(r.get("cycle_ts") or r.get("ts")) or datetime.min.replace(tzinfo=now.tzinfo),
        reverse=True,
    )
    with_rationale = [r for r in merged if str(r.get("rationale") or "").strip()]
    rows = with_rationale if with_rationale else merged

    entries: list[JournalEntry] = []
    for row in rows[:limit]:
        effect, kind = f.decision_effect(row)
        if kind == "watch":
            watch = f.safe_dict(f.safe_dict(row.get("decision")).get("indicator_watch"))
            expires = f.countdown(watch.get("expires_at"), now=now)
            if expires not in ("—", "expired"):
                effect = f"{effect} · expires {expires}"
        entries.append(
            JournalEntry(
                time=f.decision_time(row),
                symbol=str(row.get("symbol") or "—"),
                action=str(row.get("action") or "—").upper(),
                confidence=_safe_float(row.get("confidence"), default=None),
                rationale=str(row.get("rationale") or "").strip(),
                effect=effect,
                effect_kind=kind,
            )
        )
    return entries


def ledger_total(state: dict) -> int:
    """Nombre de décisions visibles dans le tail du ledger (approximation du « N more »)."""
    return len(_safe_list_of_dicts(state.get("recent_decisions")))


# ---------------------------------------------------------------------------
# NEXT TO FIRE — watches + wake scheduler fusionnés, tri ascendant
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FireItem:
    label: str  # symbole ou "wake"
    detail: str  # condition résumée / "scheduler — 2 symbols due"
    at: datetime | None
    countdown: str
    kind: str  # watch | exit | wake | session


def next_to_fire(state: dict, *, now: datetime, limit: int = 6) -> list[FireItem]:
    items: list[FireItem] = []
    for watch in _safe_list_of_dicts(state.get("indicator_watches")):
        expires = f.parse_ts(watch.get("expires_at"))
        if expires is None or expires <= now:
            continue
        is_exit = bool(f.safe_dict(watch.get("order"))) is False and str(
            watch.get("purpose") or ""
        ).startswith("exit")
        detail = f.condition_summary(watch.get("conditions"), watch.get("logic"), max_items=1, limit=30)
        items.append(
            FireItem(
                label=str(watch.get("symbol") or "—"),
                detail=(f"exit: {detail}" if is_exit else detail),
                at=expires,
                countdown=f.countdown(expires, now=now),
                kind="exit" if is_exit else "watch",
            )
        )
    wake_at = f.parse_ts(state.get("default_next_wake"))
    if wake_at is not None and wake_at > now:
        _symbols_due_raw = state.get("symbols_due")
        due = len(_symbols_due_raw) if isinstance(_symbols_due_raw, list) else 0
        due_count = sum(
            1 for ts in f.safe_dict(state.get("symbol_wakes")).values()
            if (parsed := f.parse_ts(ts)) is not None and parsed <= wake_at
        ) or due
        detail = f"scheduler — {due_count} symbols due" if due_count else "scheduler"
        items.append(
            FireItem(label="wake", detail=detail, at=wake_at, countdown=f.countdown(wake_at, now=now), kind="wake")
        )
    items.sort(key=lambda item: item.at or datetime.max.replace(tzinfo=now.tzinfo))
    return items[:limit]


# ---------------------------------------------------------------------------
# Positions / exposition
# ---------------------------------------------------------------------------


def positions_by_pnl(state: dict) -> list[dict]:
    holdings = _safe_list_of_dicts(f.safe_dict(state.get("portfolio")).get("holdings"))
    material_holdings = [
        holding
        for holding in holdings
        if abs(f.holding_quantity(holding)) > POSITION_EPSILON
    ]
    return sorted(material_holdings, key=lambda h: abs(f.holding_pnl(h)), reverse=True)


@dataclass(frozen=True)
class Exposure:
    long_usd: float = 0.0
    short_usd: float = 0.0
    by_venue: dict = field(default_factory=dict)  # {venue: gross usd}

    @property
    def gross(self) -> float:
        return self.long_usd + self.short_usd

    @property
    def net(self) -> float:
        return self.long_usd - self.short_usd


def exposure(state: dict) -> Exposure:
    from trader.market.rotation.wiring import venue_of

    long_usd = 0.0
    short_usd = 0.0
    by_venue: dict[str, float] = {}
    for holding in _safe_list_of_dicts(f.safe_dict(state.get("portfolio")).get("holdings")):
        notional = f.holding_notional(holding)
        if f.holding_quantity(holding) >= 0:
            long_usd += notional
        else:
            short_usd += notional
        try:
            venue = venue_of(f.holding_symbol(holding))
        except Exception:
            venue = "?"
        by_venue[venue] = by_venue.get(venue, 0.0) + notional
    return Exposure(long_usd=long_usd, short_usd=short_usd, by_venue=by_venue)


# ---------------------------------------------------------------------------
# Watches / plans (répartition armé vs veille)
# ---------------------------------------------------------------------------


def armed_watches(state: dict) -> list[dict]:
    return [w for w in _safe_list_of_dicts(state.get("indicator_watches")) if _is_armed_plan(w)]


def plain_watches(state: dict) -> list[dict]:
    return [w for w in _safe_list_of_dicts(state.get("indicator_watches")) if not _is_armed_plan(w)]


# ---------------------------------------------------------------------------
# Rail : vitals + badge santé
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RailVitals:
    running: bool = False
    vital_status: str = "never_started"  # alive | stopped | never_started
    heartbeat_old: bool = False
    pid: int | None = None
    dry_run: bool = True
    kill_active: bool = False
    halted: str | None = None
    session_venue: str | None = None  # venue action ouverte à afficher (ex. "TPE")
    session_open: bool = False
    clock_utc: str = ""


_VENUE_DISPLAY = {"TW": "TPE", "EU": "EU", "US": "US"}


def rail_vitals(state: dict, *, vital, kill_active: bool, now: datetime) -> RailVitals:
    status = f.safe_dict(state.get("daemon_status"))
    sessions = state.get("sessions")
    open_set = (
        open_venue_set(sessions, now) if isinstance(sessions, dict) and sessions else set()
    )
    action_open = [v for v in ("TW", "EU", "US") if v in open_set]
    session_venue = action_open[0] if action_open else None
    return RailVitals(
        running=vital.status == "alive",
        vital_status=vital.status,
        heartbeat_old=bool(getattr(vital, "battement_old", False)),
        pid=status.get("pid") if isinstance(status.get("pid"), int) else None,
        dry_run=bool(state.get("dry_run", True)),
        kill_active=kill_active,
        halted=state.get("halted") or None,
        session_venue=_VENUE_DISPLAY.get(session_venue, session_venue) if session_venue else None,
        session_open=bool(session_venue),
        clock_utc=now.strftime("%H:%M:%S UTC"),
    )


def health_alert_count(state: dict, *, kill_active: bool, now: datetime) -> int:
    """Badge ▲N du rail : nb de symboles stale + autres anomalies (hors KILL/HALT).

    KILL/HALT recolorent les vitals du rail eux-mêmes ; le badge compte le reste.
    """
    stale = f.safe_dict(state.get("stale_market_data"))
    count = len(stale)
    other = [
        item
        for item in attention_items(state, kill_active=kill_active, now=now)
        if item.severity != "crit" and not item.label.startswith("stale")
    ]
    return count + len(other)
