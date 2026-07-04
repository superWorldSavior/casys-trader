"""Agrégats purs pour la home du cockpit.

Aucune I/O, aucun import Textual — tout est calculé depuis le dict d'état
du read model (`load_runtime_state`). Testable sans UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trader.reporting.read_models.runtime_state import _safe_float, _safe_list_of_dicts

UTC = timezone.utc


def parse_ts(raw: object) -> datetime | None:
    """ISO 8601 (suffixe Z accepté) → datetime UTC-aware, sinon None."""
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decision_status(row: dict) -> str:
    """Classe une décision (logique historique de overview._decision_status)."""
    reason = str(row.get("reason") or row.get("rationale") or "").lower()
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
    source = str(row.get("decision_source") or "").lower()
    action = str(row.get("action") or "").upper()
    if row.get("executed") is True:
        return "exec"
    if reason.startswith("risk:") or "risk:" in reason:
        return "risk"
    if "stale" in reason:
        return "stale"
    if source == "armed_plan" or runtime.get("armed_plan_id"):
        return "armé"
    if runtime.get("trade_plan_created"):
        return "plan"
    if runtime.get("indicator_watch_created"):
        return "veille"
    if source == "infra" or (row.get("model_called") is False and "quiet" in reason):
        return "quiet"
    return "hold" if action == "HOLD" else "signal"


def decision_priority(row: dict) -> tuple[int, str]:
    """Score de triage (0 = plus urgent) + ts pour départager."""
    action = str(row.get("action") or "").upper()
    status = decision_status(row)
    score = 90
    if row.get("executed") is True and action in {"BUY", "SELL"}:
        score = 0
    elif action in {"BUY", "SELL"} and status in {"risk", "stale"}:
        score = 10
    elif status in {"risk", "stale"}:
        score = 20
    elif status in {"plan", "veille", "armé"}:
        score = 30
    elif action in {"BUY", "SELL"}:
        score = 40
    elif status == "quiet":
        score = 80
    return score, str(row.get("cycle_ts") or row.get("ts") or "")


def _decision_identity(row: dict) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("cycle_ts") or row.get("ts") or ""),
        str(row.get("sequence") or ""),
        str(row.get("symbol") or ""),
        str(row.get("action") or ""),
        str(row.get("reason") or row.get("rationale") or ""),
    )


def decision_source_rows(decisions: list[dict], recent_decisions: list[dict]) -> list[dict]:
    """Fusionne rapport + tail decisions.jsonl en dédupliquant par identité."""
    rows: list[dict] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in [*recent_decisions, *decisions]:
        identity = _decision_identity(row)
        if identity in seen:
            continue
        rows.append(row)
        seen.add(identity)
    return rows


def select_decision_rows(
    decisions: list[dict], recent_decisions: list[dict], *, limit: int
) -> list[dict]:
    """Top-N décisions par priorité (les plus récentes d'abord à score égal)."""
    indexed = list(enumerate(decision_source_rows(decisions, recent_decisions)))
    ranked = sorted(indexed, key=lambda item: (decision_priority(item[1])[0], -item[0]))
    return [row for _, row in ranked[:limit]]


ACTIVITY_STATES: tuple[str, ...] = ("exec", "veille", "plan", "risk", "stale", "hold")

_STATUS_TO_SERIES: dict[str, str] = {
    "exec": "exec",
    "veille": "veille",
    "armé": "plan",
    "plan": "plan",
    "risk": "risk",
    "stale": "stale",
    "quiet": "hold",
    "hold": "hold",
    "signal": "hold",
}


@dataclass(frozen=True)
class RiskAtStops:
    total_usd: float = 0.0
    worst_symbol: str | None = None
    worst_usd: float = 0.0
    without_stop: tuple[str, ...] = ()


def risk_at_stops(holdings: list[dict], trade_plans: list[dict]) -> RiskAtStops:
    """Somme USD du P&L si tous les stops se déclenchent + pire position.

    Position sans plan, sans stop ou sans prix de référence → listée dans
    without_stop, exclue du total. Jamais d'exception.
    """
    plans_by_symbol: dict[str, dict] = {}
    for plan in _safe_list_of_dicts(trade_plans):
        symbol = str(plan.get("symbol") or "")
        if symbol and symbol not in plans_by_symbol:
            plans_by_symbol[symbol] = plan

    total = 0.0
    worst_symbol: str | None = None
    worst_usd = 0.0
    without_stop: list[str] = []
    for holding in _safe_list_of_dicts(holdings):
        symbol = str(holding.get("symbol") or "?")
        plan = plans_by_symbol.get(symbol) or {}
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        reference = (
            _safe_float(holding.get("last_price"), default=None)
            or _safe_float(holding.get("avg_price"), default=None)
            or _safe_float(plan.get("entry_price"), default=None)
        )
        if stop is None or not reference:
            without_stop.append(symbol)
            continue
        qty = _safe_float(plan.get("remaining_quantity"), default=None)
        if qty is None:
            qty = _safe_float(plan.get("quantity"), default=None)
        if qty is None:
            qty = abs(_safe_float(holding.get("quantity"), default=0.0) or 0.0)
        direction = -1.0 if str(plan.get("side") or "LONG").upper() == "SHORT" else 1.0
        fx_rate = _safe_float(holding.get("fx_rate"), default=1.0) or 1.0
        risk_usd = (stop - reference) * qty * direction * fx_rate
        total += risk_usd
        if worst_symbol is None or risk_usd < worst_usd:
            worst_symbol, worst_usd = symbol, risk_usd

    return RiskAtStops(
        total_usd=total,
        worst_symbol=worst_symbol,
        worst_usd=worst_usd,
        without_stop=tuple(without_stop),
    )


@dataclass(frozen=True)
class AttentionItem:
    label: str
    severity: str  # "crit" | "warn"


def attention_items(state: dict, *, kill_active: bool, now: datetime) -> list[AttentionItem]:
    """Anomalies méritant l'attention, par criticité décroissante. [] = RAS."""
    state = state if isinstance(state, dict) else {}
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    items: list[AttentionItem] = []

    if kill_active:
        items.append(AttentionItem("KILL actif", "crit"))
    halted = state.get("halted")
    if halted:
        items.append(AttentionItem(f"HALT {halted}", "crit"))

    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    risk = risk_at_stops(
        _safe_list_of_dicts(portfolio.get("holdings")),
        _safe_list_of_dicts(state.get("trade_plans")),
    )
    if risk.total_usd < 0:
        items.append(AttentionItem(f"risque@stops {risk.total_usd:+,.0f}$", "warn"))

    stale_streaks = (
        state.get("stale_streaks") if isinstance(state.get("stale_streaks"), dict) else {}
    )
    stale = [int(v) for v in stale_streaks.values() if isinstance(v, (int, float)) and v > 0]
    if stale:
        items.append(AttentionItem(f"stale {len(stale)} (max ×{max(stale)})", "warn"))

    rejects = sum(
        1
        for row in _safe_list_of_dicts(state.get("recent_decisions"))
        if str(row.get("reason") or "").startswith("risk:")
    )
    if rejects:
        items.append(AttentionItem(f"{rejects} rejets risk", "warn"))

    expiring = 0
    for watch in _safe_list_of_dicts(state.get("armed_plans")):
        expires_at = parse_ts(watch.get("expires_at"))
        if expires_at is not None and timedelta(0) <= expires_at - now < timedelta(hours=1):
            expiring += 1
    if expiring:
        items.append(AttentionItem(f"{expiring} armé(s) expirent <1h", "warn"))

    if risk.without_stop:
        shown = ", ".join(risk.without_stop[:3])
        extra = f" +{len(risk.without_stop) - 3}" if len(risk.without_stop) > 3 else ""
        items.append(AttentionItem(f"sans stop: {shown}{extra}", "warn"))

    return items


@dataclass(frozen=True)
class VenueClock:
    open_now: tuple[str, ...] = ()
    next_venue: str | None = None
    next_kind: str | None = None  # "open" | "close"
    next_at: datetime | None = None


def venue_clock(sessions: dict, now: datetime) -> VenueClock:
    """Venues actions ouvertes + prochaine transition (open/close), UTC.

    FX (24/5) exclue. Sessions vides/malformées → VenueClock() neutre.
    """
    from trader.rotation.schedule import _close_dt, open_venues

    if not isinstance(sessions, dict) or not sessions:
        return VenueClock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    try:
        open_list = tuple(v for v in open_venues(now.isoformat(), sessions) if v != "FX")
    except Exception:
        return VenueClock()

    best: tuple[datetime, str, str] | None = None
    for venue, hours in sessions.items():
        if venue == "FX" or not isinstance(hours, dict):
            continue
        try:
            if venue in open_list:
                candidate = (_close_dt(now, str(hours["close"])), venue, "close")
            else:
                open_dt = _close_dt(now, str(hours["open"]))
                for _ in range(4):  # saute le week-end
                    if open_dt > now and open_dt.weekday() < 5:
                        break
                    open_dt = _close_dt(open_dt + timedelta(days=1), str(hours["open"]))
                candidate = (open_dt, venue, "open")
        except Exception:
            continue
        if best is None or candidate[0] < best[0]:
            best = candidate

    if best is None:
        return VenueClock(open_now=open_list)
    return VenueClock(open_now=open_list, next_venue=best[1], next_kind=best[2], next_at=best[0])


def open_venue_set(sessions: object, now: datetime) -> "set[str]":
    """Codes venue ouverts à `now` (EU/US/TW/FX). Sessions vides/invalides → set().

    Utilisé pour le badge marché ouvert/fermé des positions et du drill-down.
    Contrairement à `venue_clock`, inclut FX (24/5) car on peut détenir des paires.
    """
    from trader.rotation.schedule import open_venues

    if not isinstance(sessions, dict) or not sessions:
        return set()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        return set(open_venues(now.isoformat(), sessions))
    except Exception:
        return set()


def activity_buckets(
    recent_decisions: list[dict],
    now: datetime,
    *,
    window_min: int = 60,
    bucket_min: int = 5,
) -> dict[str, list[int]]:
    """Compte les décisions par état et tranche de temps.

    Retourne {état: [n_buckets ints]}, du plus ancien au plus récent.
    Décisions sans ts parsable, futures, ou d'âge >= window_min : ignorées.
    """
    bucket_min = max(1, bucket_min)
    n_buckets = max(1, window_min // bucket_min)
    series: dict[str, list[int]] = {state: [0] * n_buckets for state in ACTIVITY_STATES}
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    window_seconds = window_min * 60
    for row in _safe_list_of_dicts(recent_decisions):
        ts = parse_ts(row.get("cycle_ts") or row.get("ts"))
        if ts is None:
            continue
        age = (now - ts).total_seconds()
        if age < 0 or age >= window_seconds:
            continue
        index = n_buckets - 1 - int(age // (bucket_min * 60))
        target = _STATUS_TO_SERIES.get(decision_status(row), "hold")
        series[target][index] += 1
    return series
