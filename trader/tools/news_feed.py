"""news_feed — ingestion du fil d'actu (phase attribution).

v1 : Yahoo via yfinance (déjà dépendance, couvre l'univers natif EU/TW/US).
Contrat : entrée minimale (symbole, now injecté), sortie machine-readable.
Aucune décision ici — la donnée est LOGGÉE, jamais vue par l'agent en live.

Persistance des items (§3.1) :
  Après chaque fetch réussi, les items nouveaux sont appendés dans
  state/news_items/YYYY-MM-DD.jsonl (dédup uuid intra-jour, purge >60j).
  Best-effort absolu : aucune erreur d'archivage ne remonte au cycle daemon.
  L'archive est désactivée par défaut (safe pour les tests) ; le daemon
  l'active via `set_default_news_archive(state_dir / "news_items")`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

COVERAGE_OK = "ok"
COVERAGE_EMPTY = "empty"
COVERAGE_UNMAPPED = "unmapped"
COVERAGE_ERROR = "error"

_NEWS_WINDOW_DAYS = 7

# Rétention des fichiers news_items (en jours). Fichiers YYYY-MM-DD.jsonl plus vieux = purgés.
# Déclenchement : au premier append du jour (même mécanique que _purge_old_radar_cache).
NEWS_ITEMS_RETENTION_DAYS = 60

CACHE_TTL_MINUTES = 30.0
_CACHE: dict[str, tuple[datetime, dict]] = {}

# Archive des items de news (None = désactivé ; initialisé par le daemon via
# set_default_news_archive). Safe pour les tests : aucun I/O par défaut.
_NEWS_ITEMS_ARCHIVE: NewsItemsArchive | None = None


def reset_cache() -> None:
    _CACHE.clear()


@dataclass(frozen=True)
class RawNews:
    """Données brutes extraites de la source, avant classification.

    `mapped` : la source connaît-elle le symbole (sinon → unmapped).
    `earnings_dates` : dates earnings tz-aware UTC, passées ou futures.
    `news_count` : nb de news dans la fenêtre récente (déjà filtré par le fetcher).
    `items` : items bruts yfinance pour archivage. Exclu du hash/compare
    (les dicts ne sont pas hashables ; seuls les champs de décision comptent
    pour l'égalité).
    """

    mapped: bool
    earnings_dates: tuple[datetime, ...]
    news_count: int
    items: tuple[dict, ...] = field(default=(), hash=False, compare=False)


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


def _item_uuid(item: dict) -> str | None:
    """UUID unique d'un item news yfinance (None si absent).

    Cherche successivement dans item["uuid"], item["id"], puis content.id/uuid.
    """
    uid = item.get("uuid") or item.get("id")
    if uid:
        return str(uid)
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    uid = content.get("id") or content.get("uuid")
    return str(uid) if uid else None


def _item_to_row(*, symbol: str, item: dict, fetched_at: str) -> dict:
    """Transforme un item yfinance en ligne JSONL archivable.

    Champs manquants → None. Ne lève jamais (appelé dans un bloc best-effort).
    Format : {fetched_at, symbol, title, publisher, published_at, link, uuid}.
    """
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    title = item.get("title") or content.get("title")
    publisher = item.get("publisher")
    if publisher is None:
        provider = content.get("provider")
        if isinstance(provider, dict):
            publisher = provider.get("displayName")
    link = item.get("link")
    if link is None:
        canonical = content.get("canonicalUrl")
        if isinstance(canonical, dict):
            link = canonical.get("url")
    published_at_dt = _news_published_at(item)
    return {
        "fetched_at": fetched_at,
        "symbol": symbol,
        "title": title,
        "publisher": publisher,
        "published_at": published_at_dt.isoformat() if published_at_dt else None,
        "link": link,
        "uuid": _item_uuid(item),
    }


class NewsItemsArchive:
    """Persistance des items de news collectés, append-only, un fichier/jour.

    - Dédup par uuid au sein du jour : set en mémoire par instance + relecture
      du fichier du jour au premier append pour survivre au restart.
    - Purge des fichiers de plus de NEWS_ITEMS_RETENTION_DAYS jours au premier
      append du jour (même mécanique que _purge_old_radar_cache).
    - Best-effort absolu : toutes les erreurs sont avalées ; le fetch/compte
      n'est jamais affecté.
    """

    def __init__(self, base_dir: Path | str) -> None:
        self._base_dir = Path(base_dir)
        self._seen_date: str | None = None
        self._seen_uuids: set[str] = set()
        self._purge_date: str | None = None

    def _load_existing_uuids(self, date_str: str) -> set[str]:
        """Lit les uuids déjà présents dans le fichier du jour (survie au restart)."""
        path = self._base_dir / f"{date_str}.jsonl"
        uuids: set[str] = set()
        if not path.exists():
            return uuids
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    uid = row.get("uuid")
                    if uid:
                        uuids.add(str(uid))
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        return uuids

    def _purge_old(self, today: str) -> None:
        """Supprime les *.jsonl de plus de NEWS_ITEMS_RETENTION_DAYS jours.

        Noms non conformes au pattern exact YYYY-MM-DD.jsonl → ignorés.
        """
        if not self._base_dir.is_dir():
            return
        try:
            today_date = datetime.strptime(today, "%Y-%m-%d")
        except ValueError:
            return
        for f in self._base_dir.glob("*.jsonl"):
            stem = f.stem  # YYYY-MM-DD
            if len(stem) != 10 or stem[4] != "-" or stem[7] != "-":
                continue
            try:
                file_date = datetime.strptime(stem, "%Y-%m-%d")
            except ValueError:
                continue
            if (today_date - file_date).days > NEWS_ITEMS_RETENTION_DAYS:
                try:
                    f.unlink()
                except OSError:
                    pass

    def append_items(self, symbol: str, items: list[dict], *, now: datetime) -> None:
        """Append les items nouveaux (dédup uuid) dans le fichier du jour.

        Best-effort absolu : toute exception est silencieusement avalée.
        Items sans uuid → toujours appendés (pas de dédup possible).
        """
        try:
            date_str = now.strftime("%Y-%m-%d")

            # Init per-day tracking : relecture du fichier pour survivre au restart.
            if self._seen_date != date_str:
                self._seen_date = date_str
                self._seen_uuids = self._load_existing_uuids(date_str)

            # Purge une fois par jour, au premier append.
            if self._purge_date != date_str:
                self._purge_date = date_str
                self._purge_old(date_str)

            if not items:
                return

            self._base_dir.mkdir(parents=True, exist_ok=True)
            fetched_at = now.isoformat()
            path = self._base_dir / f"{date_str}.jsonl"

            new_rows: list[str] = []
            for item in items:
                uid = _item_uuid(item)
                if uid and uid in self._seen_uuids:
                    continue
                row = _item_to_row(symbol=symbol, item=item, fetched_at=fetched_at)
                new_rows.append(json.dumps(row, ensure_ascii=False))
                if uid:
                    self._seen_uuids.add(uid)

            if new_rows:
                with path.open("a", encoding="utf-8") as f:
                    for line in new_rows:
                        f.write(line + "\n")

        except Exception:
            # Best-effort : aucune erreur ne remonte au cycle daemon.
            pass


def set_default_news_archive(base_dir: Path | str) -> None:
    """Configure l'archive des items de news (appelé par le daemon au démarrage).

    Désactivée par défaut (None) pour que les tests n'écrivent jamais dans state/.
    Appeler cette fonction dans main() avec state_dir / "news_items".
    """
    global _NEWS_ITEMS_ARCHIVE
    _NEWS_ITEMS_ARCHIVE = NewsItemsArchive(Path(base_dir))


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
    symbol: str,
    *,
    now: datetime,
    fetcher: Fetcher | None = None,
    archive: NewsItemsArchive | None = None,
) -> dict:
    """Snapshot d'actu pour un symbole. NE LÈVE JAMAIS : tout échec → coverage=error.

    Cache TTL (CACHE_TTL_MINUTES) déterministe basé sur `now` ; seuls les
    snapshots réussis sont cachés (un `error` est retenté au cycle suivant).
    `fetcher` injectable pour les tests ; défaut = Yahoo via yfinance (Task 4).
    `archive` : archive des items (None = module-level default _NEWS_ITEMS_ARCHIVE,
    qui est None par défaut — safe pour les tests). Passer une instance explicite
    pour tester l'archivage ou pour override le singleton daemon.
    """
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    cached = _CACHE.get(symbol)
    if cached is not None:
        cached_now, snap = cached
        if (now_utc - cached_now).total_seconds() < CACHE_TTL_MINUTES * 60.0:
            return snap
    if fetcher is None:
        fetcher = _yahoo_fetch
    raw: RawNews
    try:
        raw = fetcher(symbol, now=now_utc)
        snap = build_snapshot(symbol, now=now_utc, raw=raw, source="yahoo")
    except Exception:
        return _error_snapshot(now_utc)
    _CACHE[symbol] = (now_utc, snap)
    # Archivage best-effort des items bruts (double protection : append_items
    # avale déjà tout, mais on isole aussi l'appel ici).
    _arc = archive if archive is not None else _NEWS_ITEMS_ARCHIVE
    if _arc is not None and raw.items:
        try:
            _arc.append_items(symbol, list(raw.items), now=now_utc)
        except Exception:
            pass
    return snap


def _yahoo_fetch(symbol: str, *, now: datetime) -> RawNews:
    """Frontière impure : interroge Yahoo via yfinance.

    Best-effort par champ : un champ qui échoue n'invalide pas les autres.
    Propage seulement si AUCUN champ ne répond (→ news_snapshot renverra error).
    Les items bruts sont collectés dans RawNews.items pour archivage.
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
    raw_items: list[dict] = []
    try:
        items = tk.news or []
        any_ok = True
        cutoff = now_utc - timedelta(days=_NEWS_WINDOW_DAYS)
        for item in items:
            raw_items.append(item)
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
    return RawNews(
        mapped=mapped,
        earnings_dates=tuple(earnings),
        news_count=news_count,
        items=tuple(raw_items),
    )
