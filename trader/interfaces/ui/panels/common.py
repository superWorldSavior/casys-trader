"""Shared helpers for Rich panel builders."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.text import Text

from trader.interfaces.ui.palette import Palette
from trader.reporting.read_models.runtime_state import UTC, _safe_float

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Mini-courbe unicode à 8 niveaux. Retourne "" si aucune valeur valide."""
    clean = [_safe_float(value, default=None) for value in values]
    clean_values = [value for value in clean if value is not None]
    if not clean_values:
        return ""

    low = min(clean_values)
    high = max(clean_values)
    if high == low:
        return "▄" * len(clean_values)

    span = high - low
    last_index = len(_SPARK_BLOCKS) - 1
    blocks: list[str] = []
    for value in clean_values:
        index = int(round((value - low) / span * last_index))
        index = max(0, min(last_index, index))
        blocks.append(_SPARK_BLOCKS[index])
    return "".join(blocks)


def _fmt_money(value: Any, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number:,.2f}"


def _fmt_signed_money(value: Any, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number:+,.2f}"


def _fmt_fee_cost(value: Any, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{-abs(number):+,.2f}"


def _holding_unrealized_pnl_for_display(holding: dict) -> float:
    after_broker_fees = _safe_float(
        holding.get("unrealized_pnl_after_broker_fees"),
        default=None,
    )
    if after_broker_fees is not None:
        return after_broker_fees
    net_pnl = _safe_float(holding.get("unrealized_pnl_net"), default=None)
    if net_pnl is not None:
        return net_pnl
    return _safe_float(holding.get("unrealized_pnl"), default=0.0) or 0.0


def _holding_round_trip_fee_for_display(holding: dict) -> float | None:
    broker_fee = _safe_float(holding.get("round_trip_broker_fee"), default=None)
    if broker_fee is not None:
        return broker_fee
    if _safe_float(holding.get("unrealized_pnl_net"), default=None) is None:
        return None
    return _safe_float(holding.get("round_trip_fee"), default=None)


def _holding_notional_usd(holding: dict) -> float:
    qty = _safe_float(holding.get("quantity"), default=0.0) or 0.0
    price = _safe_float(holding.get("last_price"), default=None)
    if price is None:
        price = _safe_float(holding.get("current_price"), default=None)
    if price is None:
        price = _safe_float(holding.get("avg_price"), default=0.0) or 0.0
    fx_rate = _safe_float(holding.get("fx_rate"), default=1.0) or 1.0
    return abs(qty) * price * fx_rate


def _fmt_number(value: Any, decimals: int = 2, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number:.{decimals}f}"


def _fmt_percent(value: Any, decimals: int = 1, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number * 100.0:.{decimals}f}%"


def _fmt_int(value: Any, *, default: str = "0") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{int(number)}"


def _style_for_pnl(value: Any) -> str:
    number = _safe_float(value, default=0.0) or 0.0
    return "green" if number >= 0 else "red"


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _market_badge(
    symbol: str, open_venues: "set[str] | None", palette: Palette
) -> Text:
    """Badge marché partagé : ● ouvert / ○ fermé / · inconnu (open_venues absent).

    ``open_venues`` = codes venue ouverts (EU/US/TW/FX). None → badge neutre,
    pour distinguer « fermé » d'« information de session indisponible ».
    """
    from trader.market.rotation.wiring import venue_of

    if open_venues is None:
        return Text("·", style=palette["dim"])
    if venue_of(symbol) in open_venues:
        return Text("●", style=palette["status_nominal"])
    return Text("○", style=palette["dim"])


def _expire_relative(expires_raw: str, *, now: datetime) -> str:
    try:
        candidate = f"{expires_raw[:-1]}+00:00" if expires_raw.endswith("Z") else expires_raw
        exp_dt = datetime.fromisoformat(candidate)
        if exp_dt.tzinfo is None:
            from datetime import timezone as _tz

            exp_dt = exp_dt.replace(tzinfo=_tz.utc)
        total_secs = int((exp_dt - now).total_seconds())
        if total_secs < 0:
            return "expiré"
        hours, rem = divmod(total_secs, 3600)
        minutes = rem // 60
        return f"dans {hours}h{minutes:02d}" if hours > 0 else f"dans {minutes}min"
    except Exception:
        return "?"


def _fmt_symbol_short(ticker: str, company_map: dict[str, str]) -> str:
    """Formate un ticker sous la forme "Nom · TICKER" tronquée à 20 chars max.

    - Si le nom est absent de company_map → retourne le ticker brut.
    - Si le nom dépasse 15 chars → utilise uniquement le premier mot.
    - Le résultat final est tronqué à 20 chars.
    """
    name = company_map.get(ticker)
    if not name:
        return ticker

    # Premier mot si nom long
    display_name = name if len(name) <= 15 else name.split()[0]
    label = f"{display_name} · {ticker}"
    return label[:20]


def _fmt_time_hms(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(candidate)
        return dt.astimezone(UTC).strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return text[:8] if text else "—"


def _fmt_price_4(value: Any) -> str:
    number = _safe_float(value, default=None)
    return "—" if number is None else f"{number:,.4f}"


def _fmt_price_pair(entry: Any, exit_: Any) -> str:
    entry_str = _fmt_price_4(entry)
    exit_str = _fmt_price_4(exit_)
    if entry_str == "—" and exit_str == "—":
        return "—"
    return f"{entry_str}→{exit_str}"


def _fmt_holding_duration(minutes: Any) -> str:
    value = _safe_float(minutes, default=None)
    if value is None or value < 0:
        return "—"
    total = int(round(value))
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours}h{mins:02d}"
    return f"{mins}m"


__all__ = [
    "_SPARK_BLOCKS",
    "_expire_relative",
    "_fmt_fee_cost",
    "_fmt_holding_duration",
    "_fmt_int",
    "_fmt_money",
    "_fmt_number",
    "_fmt_percent",
    "_fmt_price_4",
    "_fmt_price_pair",
    "_fmt_signed_money",
    "_fmt_symbol_short",
    "_fmt_time_hms",
    "_holding_notional_usd",
    "_holding_round_trip_fee_for_display",
    "_holding_unrealized_pnl_for_display",
    "_market_badge",
    "_style_for_pnl",
    "_truncate",
    "sparkline",
]
