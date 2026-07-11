"""gdelt — collecte quotidienne d'événements géopolitiques via GDELT DOC 2.0.

Source internationale gratuite et sans clé API. Zéro dépendance nouvelle : GET via
urllib, seam ``get_json`` injectable pour les tests (même pattern que
``macro_series``). GDELT limite à 1 requête / 5 s ; une seule collecte par jour suffit,
donc pas de logique de backoff ici. Fail-soft total : aucune exception ne remonte au
cycle.

Le mode ``artlist`` renvoie ``{"articles": [{url, title, seendate, domain,
sourcecountry, language, ...}]}`` — pas de champ ``tone`` (réservé aux modes
timeline/tonechart). On collecte donc titre + provenance ; le tone pourra être ajouté
plus tard via un mode dédié si besoin.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_TIMEOUT_S = 15
DEFAULT_MAX_RECORDS = 75
DEFAULT_TIMESPAN = "1d"
COLLECT_COOLDOWN_H = 20
MARKER_FILE = ".last_collect"
_MAX_TITLE_CHARS = 300

_collect_lock = threading.Lock()
_collect_in_progress: bool = False

# Query GDELT cadrée géopolitique / macro internationale. Paramétrable via l'argument
# ``query`` de collect_daily ; cette constante reste la valeur versionnée v1.
DEFAULT_QUERY = (
    '(sanctions OR tariffs OR "trade war" OR embargo OR conflict OR war OR '
    'geopolitical OR election OR "central bank" OR "interest rate") '
    "sourcelang:english"
)


def _get_json(url: str, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
    """GET → JSON dict. Lève urllib.error.URLError en cas d'erreur HTTP ou réseau."""
    req = urllib.request.Request(url, headers={"User-Agent": "casys-trader/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise urllib.error.URLError(f"HTTP {exc.code}: {body[:200]}") from exc


def _doc_url(query: str, *, maxrecords: int, timespan: str) -> str:
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": int(maxrecords),
        "timespan": timespan,
        "sort": "hybridrel",
    }
    return f"{GDELT_BASE}?{urllib.parse.urlencode(params)}"


def _normalise_article(raw: dict) -> dict | None:
    """Projette un article GDELT en ligne bornée, ou None si l'URL manque."""
    url = str(raw.get("url") or "").strip()
    if not url:
        return None
    return {
        "url": url,
        "title": str(raw.get("title") or "").strip()[:_MAX_TITLE_CHARS],
        "seendate": str(raw.get("seendate") or "").strip(),
        "domain": str(raw.get("domain") or "").strip(),
        "sourcecountry": str(raw.get("sourcecountry") or "").strip(),
        "language": str(raw.get("language") or "").strip(),
    }


def _seen_urls(path: Path) -> set[str]:
    """URLs déjà collectées (dédup). Best-effort, jamais d'exception."""
    if not path.exists():
        return set()
    seen: set[str] = set()
    try:
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            url = json.loads(line).get("url")
            if url:
                seen.add(str(url))
    except (OSError, json.JSONDecodeError, ValueError):
        return seen
    return seen


def collect_daily(
    state_dir: Path,
    now: datetime,
    *,
    get_json: Callable[[str], dict] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    query: str = DEFAULT_QUERY,
    maxrecords: int = DEFAULT_MAX_RECORDS,
    timespan: str = DEFAULT_TIMESPAN,
) -> dict:
    """Collecte les événements géopolitiques GDELT récents et les append en JSONL.

    Une seule requête bornée (maxrecords, timespan) → normalisation → dédup par ``url``
    contre ``state/gdelt/events.jsonl`` → append. Toute erreur réseau/format est avalée
    (fail-soft) : retourne ``{"collected", "skipped", "errors"}``.

      get_json — injectable pour les tests (fn(url) → dict) ; None = _get_json.
    """
    _fetch = get_json if get_json is not None else lambda url: _get_json(url, timeout_s=timeout_s)

    gdelt_dir = Path(state_dir) / "gdelt"
    gdelt_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = gdelt_dir / "events.jsonl"

    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    ts_collected = now_utc.isoformat()

    try:
        data = _fetch(_doc_url(query, maxrecords=maxrecords, timespan=timespan))
    except Exception as exc:  # noqa: BLE001 — fail-soft total, jamais de remontée
        log.warning("gdelt: fetch échoué : %s", exc)
        return {"collected": 0, "skipped": 0, "errors": 1}

    articles = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(articles, list):
        log.warning("gdelt: réponse sans liste 'articles'")
        return {"collected": 0, "skipped": 0, "errors": 1}

    seen = _seen_urls(jsonl_path)
    collected = skipped = 0
    with jsonl_path.open("a", encoding="utf-8") as fh:
        for raw in articles[: int(maxrecords)]:
            if not isinstance(raw, dict):
                continue
            item = _normalise_article(raw)
            if item is None:
                continue
            if item["url"] in seen:
                skipped += 1
                continue
            seen.add(item["url"])
            fh.write(json.dumps({"ts_collected": ts_collected, **item}) + "\n")
            collected += 1

    log.info("gdelt: +%d événements (skip %d)", collected, skipped)
    return {"collected": collected, "skipped": skipped, "errors": 0}


# ---------------------------------------------------------------------------
# Déclenchement best-effort — thread daemon fire-and-forget (pattern macro_series)
# ---------------------------------------------------------------------------


def _read_last_collect(marker: Path) -> datetime | None:
    try:
        raw = marker.read_text("utf-8").strip()
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (OSError, ValueError):
        return None


def _write_last_collect(marker: Path, now_utc: datetime) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(now_utc.isoformat(), encoding="utf-8")
    except OSError as exc:
        log.warning("gdelt: écriture marqueur échouée : %s", exc)


def maybe_collect(
    state_dir: Path,
    now: datetime,
    *,
    get_json: Callable[[str], dict] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cooldown_h: float = COLLECT_COOLDOWN_H,
) -> dict:
    """Lance collect_daily dans un thread daemon fire-and-forget si cooldown dépassé.

    Retourne immédiatement — le cycle n'est jamais bloqué. Marqueur posé au lancement
    du thread ; verrou module pour un seul thread à la fois. Best-effort total.
    """
    global _collect_in_progress

    gdelt_dir = Path(state_dir) / "gdelt"
    marker = gdelt_dir / MARKER_FILE
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)

    last = _read_last_collect(marker)
    if last is not None:
        elapsed_h = (now_utc - last).total_seconds() / 3600.0
        if elapsed_h < cooldown_h:
            return {"triggered": False, "reason": "cooldown", "elapsed_h": round(elapsed_h, 2)}

    with _collect_lock:
        if _collect_in_progress:
            return {"triggered": False, "reason": "in_progress"}
        _collect_in_progress = True

    _write_last_collect(marker, now_utc)

    def _run() -> None:
        global _collect_in_progress
        try:
            collect_daily(state_dir, now, get_json=get_json, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001 — best-effort total, jamais de remontée
            log.warning("gdelt: échec inattendu collect_daily : %s", exc)
        finally:
            with _collect_lock:
                _collect_in_progress = False

    thread = threading.Thread(target=_run, daemon=True, name="gdelt-collect")
    thread.start()
    return {"triggered": True, "_thread": thread}


__all__ = ["collect_daily", "maybe_collect", "DEFAULT_QUERY", "GDELT_BASE"]
