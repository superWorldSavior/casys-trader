"""Client HTTP direct Yahoo Finance — endpoint public v8 chart.

Frontière I/O PURE : parle à l'endpoint v8 et traduit la réponse JSON en barres
brutes. Ne juge JAMAIS une barre individuelle (close<=0, NaN) — c'est la
responsabilité du métier en aval (`market_data_yf.get_bars`). L'ajustement OHLC
reste opt-in pour les consommateurs historiques comme le radar. Un `null` Yahoo
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
    """Barre Yahoo non jugée : open/high/low/close/volume peuvent être NaN."""

    ts: str  # ISO 8601, timezone de la place de cotation
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
    auto_adjust: bool = False,
    http_get: Callable[[str], str] = _default_http_get,
) -> list[RawBar]:
    """Barres depuis Yahoo v8 chart. `null`→NaN, aucun jugement de valeur.

    Args:
        symbol: Symbole Yahoo natif (ex. ``ACA.PA``, ``2330.TW``, ``AAPL``).
        lookback: Fenêtre → param ``range`` v8 (``1d``, ``5d``, ``1mo``…).
        interval: Granularité → param ``interval`` v8 (``1h``, ``1d``…).
        auto_adjust: Reproduit l'ajustement yfinance ``Adj Close / Close`` sur
            OHLC. Désactivé par défaut afin que les prix runtime restent bruts.
        http_get: Transport injectable (défaut = urllib). Sert aux tests.

    Returns:
        Barres dans l'ordre chronologique. Une barre par timestamp renvoyé,
        valeurs manquantes (``null``) traduites en NaN — jamais filtrées ici.
        Avec ``auto_adjust=True``, OHLC suit exactement le ratio
        ``Adj Close / Close`` utilisé historiquement par yfinance.
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
        indicators = result.get("indicators") or {}
        quote = ((indicators.get("quote") or [{}]) or [{}])[0]
        opens, highs = quote.get("open") or [], quote.get("high") or []
        lows, closes, volumes = quote.get("low") or [], quote.get("close") or [], quote.get("volume") or []
        raw_adjcloses = ((indicators.get("adjclose") or [{}]) or [{}])[0].get(
            "adjclose"
        )
        adjcloses = raw_adjcloses if isinstance(raw_adjcloses, list) and raw_adjcloses else None

        return [
            _bar_at(
                timestamp=int(ts),
                tz=tz,
                index=i,
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                volumes=volumes,
                adjcloses=adjcloses,
                auto_adjust=auto_adjust,
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


def _bar_at(
    *,
    timestamp: int,
    tz: timezone | ZoneInfo,
    index: int,
    opens: list,
    highs: list,
    lows: list,
    closes: list,
    volumes: list,
    adjcloses: list | None,
    auto_adjust: bool,
) -> RawBar:
    raw_open = _at(opens, index)
    raw_high = _at(highs, index)
    raw_low = _at(lows, index)
    raw_close = _at(closes, index)
    if auto_adjust:
        adjusted_close = _at(adjcloses, index) if adjcloses is not None else raw_close
        ratio = adjusted_close / raw_close if raw_close else math.nan
        open_value = raw_open * ratio
        high_value = raw_high * ratio
        low_value = raw_low * ratio
        close_value = adjusted_close
    else:
        open_value = raw_open
        high_value = raw_high
        low_value = raw_low
        close_value = raw_close
    return RawBar(
        ts=datetime.fromtimestamp(timestamp, tz=tz).isoformat(),
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=_at(volumes, index),
    )


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
