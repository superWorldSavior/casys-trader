"""format — formatage pur pour les pages du cockpit « casys » (UI anglaise).

Fonctions PURES (valeur → str/Text), aucune I/O, aucun import Textual.
Conventions du design « Decision Journal » :

- countdowns : ``3h58`` / ``14m`` / ``expired`` ; préfixe ``in `` via kwarg.
- devises : USD implicite, ``$100,053`` sans décimales pour l'equity.
- glossaire : veille → watch · plan armé → armed order · sortie → exit plan.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rich.text import Text

from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_METER_EMPTY,
)
from trader.market import fx
from trader.reporting.read_models.runtime_state import _safe_float, _safe_list_of_dicts


def safe_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


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
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Nombres
# ---------------------------------------------------------------------------


def fmt_compact(value: object, *, decimals: int = 2, default: str = "—") -> str:
    number = _safe_float(value, default=None)
    return f"{number:,.{decimals}f}" if number is not None else default


def fmt_signed(value: object, *, decimals: int = 0, default: str = "—") -> str:
    number = _safe_float(value, default=None)
    return f"{number:+,.{decimals}f}" if number is not None else default


def fmt_money(value: object, *, decimals: int = 0, default: str = "—") -> str:
    number = _safe_float(value, default=None)
    return f"${number:,.{decimals}f}" if number is not None else default


def fmt_signed_money(value: object, *, decimals: int = 0, default: str = "—") -> str:
    number = _safe_float(value, default=None)
    if number is None:
        return default
    sign = "+" if number >= 0 else "−"
    return f"{sign}${abs(number):,.{decimals}f}"


def fmt_pct(value: object, *, decimals: int = 1, signed: bool = True, default: str = "—") -> str:
    """value déjà en points de pourcentage (5.35 → "+5.35%")."""
    number = _safe_float(value, default=None)
    if number is None:
        return default
    return f"{number:+.{decimals}f}%" if signed else f"{number:.{decimals}f}%"


def clip(value: object, *, limit: int = 32) -> str:
    text = str(value or "—").replace("\n", " ")
    return text if len(text) <= limit else f"{text[: max(0, limit - 1)]}…"


# ---------------------------------------------------------------------------
# Temps
# ---------------------------------------------------------------------------


def hhmm(raw: object) -> str:
    """Timestamp ISO → "HH:MM" UTC, "—" si invalide."""
    parsed = parse_ts(raw)
    return parsed.strftime("%H:%M") if parsed else "—"


def hhmmss(raw: object) -> str:
    parsed = parse_ts(raw)
    return parsed.strftime("%H:%M:%S") if parsed else "—"


def countdown(target: object, *, now: datetime, prefix: str = "") -> str:
    """Durée jusqu'à ``target`` : "3h58" / "14m" / "now" / "expired" / "—".

    ``prefix`` ("in ") est appliqué aux valeurs futures uniquement.
    """
    expires_at = target if isinstance(target, datetime) else parse_ts(target)
    if expires_at is None:
        return "—"
    # normalise naive → UTC pour éviter TypeError quand now est aware
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    total_secs = int((expires_at - now).total_seconds())
    if total_secs < -30:
        return "expired"
    if total_secs < 60:
        return f"{prefix}now"
    hours, rem = divmod(total_secs, 3600)
    minutes = rem // 60
    label = f"{hours}h{minutes:02d}" if hours else f"{minutes}m"
    return f"{prefix}{label}"


def duration_m(minutes: object, *, default: str = "—") -> str:
    """Minutes → "2d1h" / "3h05" / "43m"."""
    number = _safe_float(minutes, default=None)
    if number is None:
        return default
    total = int(number)
    days, rem = divmod(total, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    return f"{hours}h{mins:02d}" if hours else f"{mins}m"


def age_m(minutes: object, *, default: str = "—") -> str:
    """Âge de données : "3m" / "78h" (heures dès 100 min)."""
    number = _safe_float(minutes, default=None)
    if number is None:
        return default
    if number < 100:
        return f"{int(number)}m"
    return f"{int(number / 60)}h"


def ttl_fraction(watch: dict, *, now: datetime) -> float:
    """Part de TTL restante ∈ [0, 1] : 1 = tout le temps restant."""
    created = parse_ts(watch.get("created_at"))
    expires = parse_ts(watch.get("expires_at"))
    if created is None or expires is None or expires <= created:
        return 0.0
    remaining = (expires - now).total_seconds() / (expires - created).total_seconds()
    return max(0.0, min(1.0, remaining))


# ---------------------------------------------------------------------------
# Éléments visuels
# ---------------------------------------------------------------------------


def conf_meter(confidence: object, *, width: int = 10) -> Text:
    """Meter ▮▮▮▮ accent/vide + score ("0.72")."""
    number = _safe_float(confidence, default=None)
    text = Text()
    if number is None:
        text.append("▮" * width, style=CASYS_METER_EMPTY)
        text.append("  —")
        return text
    filled = max(0, min(width, round(number * width)))
    text.append("▮" * filled, style=CASYS_ACCENT)
    text.append("▮" * (width - filled), style=CASYS_METER_EMPTY)
    text.append(f"  {number:.2f}")
    return text


def bar(value: float, total: float, *, width: int = 8) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(abs(value) / total * width)))
    return "█" * filled + "░" * (width - filled)


def signed_bar(value: float, total_abs: float, *, width: int = 8) -> str:
    prefix = "+" if value >= 0 else "-"
    return prefix + bar(value, total_abs, width=width)


def ttl_bar(fraction: float, *, width: int = 8) -> str:
    """Barre ▮▮▮░░ : part restante du TTL."""
    filled = max(0, min(width, round(fraction * width)))
    return "▮" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------


def holding_symbol(holding: dict) -> str:
    return str(holding.get("symbol") or "—")


def holding_currency(holding: dict) -> str:
    try:
        return fx.currency_for(holding_symbol(holding))
    except Exception:
        return "USD"


def holding_quantity(holding: dict) -> float:
    return _safe_float(holding.get("quantity"), default=0.0) or 0.0


def holding_native_price(holding: dict) -> float | None:
    return _safe_float(holding.get("last_price"), default=None) or _safe_float(
        holding.get("avg_price"), default=None
    )


def holding_notional(holding: dict) -> float:
    qty = holding_quantity(holding)
    price = holding_native_price(holding) or 0.0
    fx_rate = _safe_float(holding.get("fx_rate"), default=1.0) or 1.0
    return abs(qty * price * fx_rate)


def holding_pnl(holding: dict) -> float:
    net = _safe_float(holding.get("unrealized_pnl_net"), default=None)
    if net is not None:
        return net
    return _safe_float(holding.get("unrealized_pnl"), default=0.0) or 0.0


def price_for_symbol(state: dict, symbol: str) -> float | None:
    raw = safe_dict(state.get("prices")).get(symbol)
    if isinstance(raw, dict):
        for key in ("price", "last", "last_price", "close"):
            price = _safe_float(raw.get(key), default=None)
            if price is not None:
                return price
        return None
    return _safe_float(raw, default=None)


def equity_curve(state: dict) -> list[float]:
    return [
        value
        for value in (_safe_float(item, default=None) for item in (state.get("equity_curve") or []))
        if value is not None
    ]


def symbol_is_stale(state: dict, symbol: str) -> bool:
    """PRÉSENCE de la clé dans stale_market_data = stale (même entrée vide)."""
    stale_market_data = safe_dict(state.get("stale_market_data"))
    stale_streaks = safe_dict(state.get("stale_streaks"))
    return symbol in stale_market_data or (
        (_safe_float(stale_streaks.get(symbol), default=0.0) or 0.0) > 0.0
    )


def staleness_age_m(state: dict, symbol: str) -> float | None:
    """Âge des données en minutes si le symbole est stale, sinon None (fresh)."""
    entry = safe_dict(safe_dict(state.get("stale_market_data")).get(symbol))
    return _safe_float(entry.get("data_age_minutes"), default=None)


# ---------------------------------------------------------------------------
# Plans / watches
# ---------------------------------------------------------------------------


def first_take_profit(plan: dict) -> float | None:
    for take_profit in _safe_list_of_dicts(plan.get("take_profits")):
        price = _safe_float(take_profit.get("price"), default=None)
        if price is not None:
            return price
    return None


def plan_for_symbol(trade_plans: list[dict], symbol: str) -> dict:
    for plan in trade_plans:
        if str(plan.get("symbol") or "") == symbol:
            return plan
    return {}


def stop_distance_pct(plan: dict, reference: float | None) -> float | None:
    """Position du stop vs prix en % BRUT : (stop − ref) / ref.

    Convention du design (uniforme sur toutes les pages) : négatif = stop
    sous le prix (LONG typique), positif = stop au-dessus (SHORT typique).
    La proximité du stop se juge sur ``abs()``.
    """
    stop = _safe_float(plan.get("hard_stop_price"), default=None)
    if stop is None or not reference:
        return None
    return (stop - reference) / reference * 100.0


def protect_label(plan: dict) -> str:
    """PROTECT du design : "trail 3.5%" / "prot 0.5R" / "breakeven" / "—"."""
    trailing = plan.get("trailing_stop")
    if isinstance(trailing, dict):
        pct = _safe_float(
            trailing.get("trail_pct") or trailing.get("pct") or trailing.get("distance_pct"),
            default=None,
        )
        return f"trail {pct:g}%" if pct is not None else "trail"
    if trailing:
        return "trail"
    protection = plan.get("profit_protection")
    if isinstance(protection, dict):
        r_mult = _safe_float(
            protection.get("lock_r") or protection.get("r_multiple") or protection.get("at_r"),
            default=None,
        )
        if r_mult is not None:
            return f"prot {r_mult:g}R"
        if protection.get("breakeven") or str(protection.get("mode") or "") == "breakeven":
            return "breakeven"
        return "prot"
    if protection:
        return "prot"
    return "—"


def condition_summary(conditions: object, logic: object, *, max_items: int = 2, limit: int = 36) -> str:
    rows = _safe_list_of_dicts(conditions)
    parts: list[str] = []
    for condition in rows[:max_items]:
        indicator = str(condition.get("indicator") or "?")
        op = str(condition.get("op") or "?")
        value = condition.get("value")
        timeframe = str(condition.get("timeframe") or condition.get("interval") or "?")
        parts.append(f"{indicator} {op} {value if value is not None else '?'} @{timeframe}")
    if not parts:
        return "—"
    if len(rows) > max_items:
        parts.append(f"+{len(rows) - max_items}")
    separator = f" {logic or 'any'} "
    return clip(separator.join(parts), limit=limit)


def armed_order_label(watch: dict) -> str:
    order = safe_dict(watch.get("order"))
    action = str(order.get("action") or order.get("intent") or "ORDER").upper()
    qty = _safe_float(order.get("qty"), default=None)
    return clip(f"{action} {qty:g}" if qty is not None else action, limit=18)


def armed_stop_price(watch: dict) -> float | None:
    order = safe_dict(watch.get("order"))
    hard_stop = safe_dict(order.get("exit_plan")).get("hard_stop")
    if isinstance(hard_stop, dict):
        hard_stop = hard_stop.get("price")
    return _safe_float(hard_stop, default=None)


# ---------------------------------------------------------------------------
# Décisions
# ---------------------------------------------------------------------------


def decision_time(row: dict) -> str:
    return hhmm(row.get("cycle_ts") or row.get("ts"))


def decision_effect(row: dict) -> tuple[str, str]:
    """(texte, genre) — genre ∈ fill|watch|plan|wake|none, pour le style.

    Textes du design : "filled 17.5 @ 1,230" · "watch: z ≤ 0.9 @1h · expires 2h58"
    (l'expiration est ajoutée par l'appelant quand il connaît ``now``).
    """
    runtime = safe_dict(row.get("runtime"))
    if row.get("executed") is True:
        qty = row.get("qty") or row.get("quantity")
        price = _safe_float(row.get("price"), default=None)
        qty_part = f" {qty:g}" if isinstance(qty, (int, float)) else (f" {qty}" if qty else "")
        price_part = f" @ {fmt_compact(price, decimals=2)}" if price is not None else ""
        return f"filled{qty_part}{price_part}", "fill"
    if runtime.get("armed_plan_id") or str(row.get("decision_source") or "") == "armed_plan":
        return "armed order", "plan"
    if runtime.get("indicator_watch_created"):
        watch = safe_dict(safe_dict(row.get("decision")).get("indicator_watch"))
        summary = condition_summary(watch.get("conditions"), watch.get("logic"), max_items=1)
        return (f"watch: {summary}" if summary != "—" else "watch created"), "watch"
    if runtime.get("trade_plan_created"):
        return "exit plan set", "plan"
    if runtime.get("next_wake_requested") or runtime.get("next_wake_event"):
        return "wake set", "wake"
    reason = str(row.get("reason") or "")
    if reason.startswith("risk:"):
        return f"risk gate: {reason.removeprefix('risk:')}", "risk"
    return clip(row.get("reason") or row.get("rationale"), limit=40), "none"


def decision_source_label(row: dict) -> str:
    runtime = safe_dict(row.get("runtime"))
    provider = row.get("llm_provider") or runtime.get("llm_provider")
    model = row.get("llm_model") or runtime.get("llm_model")
    if provider or model:
        return clip(f"{provider or 'llm'}:{model or '?'}", limit=18)
    source = row.get("decision_source")
    if source:
        return clip(source, limit=18)
    return clip(row.get("data_source") or runtime.get("data_source"), limit=18)
