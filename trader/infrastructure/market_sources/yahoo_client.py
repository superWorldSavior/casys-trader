"""Client HTTP direct Yahoo Finance — endpoint public v8 chart (barres brutes).

Frontière I/O PURE : parle à l'endpoint v8 et traduit la réponse JSON en barres
brutes. Ne juge JAMAIS une barre individuelle (close<=0, NaN) — c'est la
responsabilité du métier en aval (`market_data_yf.get_bars`). Un `null` Yahoo
est traduit *fidèlement* en NaN (traduction, pas jugement) ; seule l'absence
totale de données, une erreur de la réponse ou un échec de transport lève
MarketError. Résultat : la validité d'une barre reste décidée à un SEUL endroit.

Remplace la dépendance `yfinance` : même source (endpoint `v8/finance/chart`
que le package tape en interne), mais contrôle total des erreurs (mappées en
MarketError), zéro cache/threading caché, aucune dépendance externe (urllib
stdlib). L'endpoint ne requiert ni clé ni crumb.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trader.domain.market_data import MarketError

_V8_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
_UA = "Mozilla/5.0 (compatible; casys-trader/1.0)"
_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class RawBar:
    """Barre brute Yahoo, non jugée : open/high/low/close/volume peuvent être NaN."""

    ts: str  # ISO 8601, tz de la place de cotation (parité yfinance auto_adjust=False)
    open: float
    high: float
    low: float
    close: float
    volume: float


def _default_http_get(url: str, *, timeout: float = _TIMEOUT_S) -> str:
    req = Request(url, headers={"User-Agent": _UA})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — schéma https fixe
        return resp.read().decode("utf-8")


def fetch_ohlc(
    symbol: str,
    lookback: str,
    interval: str,
    *,
    http_get: Callable[[str], str] = _default_http_get,
) -> list[RawBar]:
    """Barres brutes depuis Yahoo v8 chart. `null`→NaN, aucun jugement de valeur.

    Args:
        symbol: Symbole Yahoo natif (ex. ``ACA.PA``, ``2330.TW``, ``AAPL``).
        lookback: Fenêtre → param ``range`` v8 (``1d``, ``5d``, ``1mo``…).
        interval: Granularité → param ``interval`` v8 (``1h``, ``1d``…).
        http_get: Transport injectable (défaut = urllib). Sert aux tests.

    Returns:
        Barres dans l'ordre chronologique. Une barre par timestamp renvoyé,
        valeurs manquantes (``null``) traduites en NaN — jamais filtrées ici.
        Si ``indicators.quote`` est absent/vide alors que des timestamps
        existent, les barres sont émises entièrement en NaN (le client ne juge
        pas) ; le métier en aval (``market_data_yf.get_bars``) les rejette et
        conclut ``no_data``.

    Raises:
        MarketError('no_data'): réponse valide mais sans aucune barre.
        MarketError('fetch_failed'): transport KO (y compris coupure en cours
            de lecture / décodage), JSON illisible, ``chart.error`` non-null, ou
            réponse v8 de forme aberrante. Aucune exception brute ne fuit.
    """
    try:
        body = http_get(f"{_V8_CHART}{symbol}?range={lookback}&interval={interval}")
        chart = json.loads(body)["chart"]

        error = chart.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            raise MarketError("fetch_failed", f"{symbol}: chart.error={code or error}")

        results = chart.get("result") or []
        if not results:
            raise MarketError("no_data", f"{symbol} (range={lookback}, interval={interval})")

        result = results[0]
        timestamps = result.get("timestamp") or []
        if not timestamps:
            raise MarketError("no_data", f"{symbol}: aucune barre (range={lookback}, interval={interval})")

        tz = _exchange_tz(result.get("meta") or {})
        quote = (((result.get("indicators") or {}).get("quote") or [{}]) or [{}])[0]
        opens, highs = quote.get("open") or [], quote.get("high") or []
        lows, closes, volumes = quote.get("low") or [], quote.get("close") or [], quote.get("volume") or []

        return [
            RawBar(
                ts=datetime.fromtimestamp(int(ts), tz=tz).isoformat(),
                open=_at(opens, i),
                high=_at(highs, i),
                low=_at(lows, i),
                close=_at(closes, i),
                volume=_at(volumes, i),
            )
            for i, ts in enumerate(timestamps)
        ]
    except MarketError:
        raise
    except Exception as exc:  # noqa: BLE001 — frontière externe : transport (coupure
        # en cours de read/décodage), JSON illisible ou réponse v8 de forme aberrante
        # → fetch_failed. Parité avec l'ancien chemin yfinance (`except Exception`) :
        # aucune exception brute ne remonte à l'appelant.
        raise MarketError("fetch_failed", f"{symbol}: {exc}") from exc


def _at(arr: list, i: int) -> float:
    """Valeur ``i`` d'un tableau parallèle ; absente ou ``null`` → NaN."""
    if i >= len(arr):
        return math.nan
    value = arr[i]
    return math.nan if value is None else float(value)


def _exchange_tz(meta: dict) -> timezone | ZoneInfo:
    """Tz de la place (parité timestamps yfinance) ; défaut UTC si absente/inconnue."""
    name = meta.get("exchangeTimezoneName")
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc
