"""news_feed — ingestion du fil d'actu (phase attribution).

v1 : Yahoo via yfinance (déjà dépendance, couvre l'univers natif EU/TW/US).
Contrat : entrée minimale (symbole, now injecté), sortie machine-readable.
Aucune décision ici — la donnée est LOGGÉE, jamais vue par l'agent en live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

COVERAGE_OK = "ok"
COVERAGE_EMPTY = "empty"
COVERAGE_UNMAPPED = "unmapped"
COVERAGE_ERROR = "error"

_NEWS_WINDOW_DAYS = 7


@dataclass(frozen=True)
class RawNews:
    """Données brutes extraites de la source, avant classification.

    `mapped` : la source connaît-elle le symbole (sinon → unmapped).
    `earnings_dates` : dates earnings tz-aware UTC, passées ou futures.
    `news_count` : nb de news dans la fenêtre récente (déjà filtré par le fetcher).
    """

    mapped: bool
    earnings_dates: tuple[datetime, ...]
    news_count: int


def _next_future_earnings(now: datetime, dates) -> datetime | None:
    future = [d for d in dates if d > now]
    return min(future) if future else None


def _earnings_in_h(now: datetime, next_dt: datetime | None) -> float | None:
    if next_dt is None:
        return None
    return round((next_dt - now).total_seconds() / 3600.0, 2)


def _classify_coverage(raw: RawNews) -> str:
    if not raw.mapped:
        return COVERAGE_UNMAPPED
    if raw.news_count > 0:
        return COVERAGE_OK
    return COVERAGE_EMPTY


def build_snapshot(symbol: str, *, now: datetime, raw: RawNews, source: str) -> dict:
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    next_earnings = _next_future_earnings(now_utc, raw.earnings_dates)
    return {
        "earnings_in_h": _earnings_in_h(now_utc, next_earnings),
        "news_coverage": _classify_coverage(raw),
        "news_count": raw.news_count,
        "source": source,
        "asof": now_utc.astimezone(timezone.utc).isoformat(),
    }


Fetcher = Callable[..., RawNews]


def _error_snapshot(now: datetime) -> dict:
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return {
        "earnings_in_h": None,
        "news_coverage": COVERAGE_ERROR,
        "news_count": 0,
        "source": "none",
        "asof": now_utc.astimezone(timezone.utc).isoformat(),
    }


def news_snapshot(
    symbol: str, *, now: datetime, fetcher: Fetcher | None = None
) -> dict:
    """Snapshot d'actu pour un symbole. NE LÈVE JAMAIS : tout échec → coverage=error.

    `fetcher` injectable pour les tests ; défaut = Yahoo via yfinance (Task 4).
    """
    if fetcher is None:
        fetcher = _yahoo_fetch  # type: ignore[name-defined]  # défini en Task 4
    try:
        raw = fetcher(symbol, now=now)
        return build_snapshot(symbol, now=now, raw=raw, source="yahoo")
    except Exception:
        return _error_snapshot(now)
