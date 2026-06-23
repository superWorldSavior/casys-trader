"""news_feed — ingestion du fil d'actu (phase attribution).

v1 : Yahoo via yfinance (déjà dépendance, couvre l'univers natif EU/TW/US).
Contrat : entrée minimale (symbole, now injecté), sortie machine-readable.
Aucune décision ici — la donnée est LOGGÉE, jamais vue par l'agent en live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

COVERAGE_OK = "ok"
COVERAGE_EMPTY = "empty"
COVERAGE_UNMAPPED = "unmapped"
COVERAGE_ERROR = "error"

_NEWS_WINDOW_DAYS = 7

CACHE_TTL_MINUTES = 30.0
_CACHE: dict[str, tuple[datetime, dict]] = {}


def reset_cache() -> None:
    _CACHE.clear()


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


def _news_published_at(item: dict) -> datetime | None:
    """Date de publication d'un item news yfinance, en UTC, ou None.

    yfinance ≥1.4 : `item["content"]["pubDate"]` (ISO 8601). Legacy :
    `item["providerPublishTime"]` (epoch). None si aucune date exploitable.
    """
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    raw = content.get("pubDate") or content.get("displayTime")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    epoch = item.get("providerPublishTime")
    if epoch is not None:
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return None


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

    Cache TTL (CACHE_TTL_MINUTES) déterministe basé sur `now` ; seuls les
    snapshots réussis sont cachés (un `error` est retenté au cycle suivant).
    `fetcher` injectable pour les tests ; défaut = Yahoo via yfinance (Task 4).
    """
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    cached = _CACHE.get(symbol)
    if cached is not None:
        cached_now, snap = cached
        if (now_utc - cached_now).total_seconds() < CACHE_TTL_MINUTES * 60.0:
            return snap
    if fetcher is None:
        fetcher = _yahoo_fetch
    try:
        raw = fetcher(symbol, now=now_utc)
        snap = build_snapshot(symbol, now=now_utc, raw=raw, source="yahoo")
    except Exception:
        return _error_snapshot(now_utc)
    _CACHE[symbol] = (now_utc, snap)
    return snap


def _yahoo_fetch(symbol: str, *, now: datetime) -> RawNews:
    """Frontière impure : interroge Yahoo via yfinance.

    Best-effort par champ : un champ qui échoue n'invalide pas les autres.
    Propage seulement si AUCUN champ ne répond (→ news_snapshot renverra error).
    """
    import yfinance as yf

    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    tk = yf.Ticker(symbol)
    any_ok = False

    mapped = False
    try:
        last = tk.fast_info["last_price"]
        mapped = last is not None
        any_ok = True
    except (KeyError, Exception):
        pass

    earnings: list[datetime] = []
    try:
        df = tk.get_earnings_dates(limit=12)
        any_ok = True
        if df is not None and getattr(df, "index", None) is not None:
            for ts in df.index:
                dt = ts.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                earnings.append(dt.astimezone(timezone.utc))
    except Exception:
        pass

    news_count = 0
    try:
        items = tk.news or []
        any_ok = True
        cutoff = now_utc - timedelta(days=_NEWS_WINDOW_DAYS)
        for item in items:
            published = _news_published_at(item)
            if published is None:
                news_count += 1  # pas de date exploitable → compté par prudence
            elif published >= cutoff:
                news_count += 1
    except Exception:
        pass

    # Règle défensive : si Yahoo a retourné des news ou des earnings, le symbole
    # est forcément connu — même si fast_info["last_price"] a échoué ou vaut None.
    # Sémantique : unmapped SEULEMENT quand AUCUN signal (ni prix, ni news, ni earnings).
    mapped = mapped or news_count > 0 or bool(earnings)

    if not any_ok:
        raise RuntimeError(f"yahoo unreachable for {symbol}")
    return RawNews(mapped=mapped, earnings_dates=tuple(earnings), news_count=news_count)
