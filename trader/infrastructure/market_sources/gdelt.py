"""Collecteur GDELT best-effort via bulk download (l'API DOC est throttlée).

L'API DOC 2.0 répond HTTP 429 persistant depuis ~2026-08-24 (throttle par IP
côté GDELT, indépendant de notre volume d'1 req/jour). Ce collecteur
télécharge à la place les exports bruts GKG 15-min (data.gdeltproject.org,
sans clé ni quota) : 8 slots étalés sur 24h, filtrés sur les thèmes.

Même contrat que l'ancien collecteur : append-only vers
state/gdelt/events.jsonl, dédup par URL (idempotent même si un export est
retraité), cooldown 20h, fail-soft total. Les lignes bulk portent
themes+tone au lieu d'un titre (le bulk ne fournit pas les titres en
clair) ; le lecteur briefs les consomme telles quelles.

Note : GDELT sert le bulk en HTTP plain (pas de HTTPS). Acceptable pour
des briefs best-effort ; les chunks sont bornés en taille et parsés en
positionnel strict (lignes courtes ignorées).
"""

from __future__ import annotations

import io
import json
import logging
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

GDELT_DIR = "gdelt"
EVENTS_FILE = "events.jsonl"
MARKER_FILE = ".last_collect"
DEFAULT_COOLDOWN_H = 20.0
DEFAULT_TIMEOUT_S = 25

BULK_BASE = "http://data.gdeltproject.org/gdeltv2"
CHUNK_STEP_H = 3
CHUNK_COUNT = 8
MAX_CHUNK_BYTES = 15_000_000
MAX_NEW_ROWS = 400

# Sous-chaînes (minuscules) matched against the ";"-joined GKG themes blob.
THEME_KEYWORDS = (
    "sanctions",
    "tariffs",
    "trade war",
    "embargo",
    "conflict",
    "war",
    "geopolitical",
    "election",
    "central bank",
    "interest rate",
)

# GKG 2.0 fixed column indexes (headerless file, verified 2026-09-18 against
# a live chunk: 27 columns, tone at 15, 13-14 empty in this file version).
_GKG_DATE = 1
_GKG_SOURCE = 3
_GKG_URL = 4
_GKG_THEMES = 7
_GKG_TONE = 15
_GKG_MIN_COLS = 16

__all__ = ["collect_daily", "maybe_collect", "THEME_KEYWORDS"]


def _chunk_stamps(now: datetime) -> list[str]:
    """8 slots 15-min alignés, étalés sur 24h (now, now-3h, ..., now-21h)."""
    base = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    base -= timedelta(minutes=base.minute % 15)
    stamps: list[str] = []
    for step in range(CHUNK_COUNT):
        stamp = (base - timedelta(hours=step * CHUNK_STEP_H)).strftime("%Y%m%d%H%M00")
        if stamp not in stamps:
            stamps.append(stamp)
    return stamps


def _chunk_url(stamp: str) -> str:
    return f"{BULK_BASE}/{stamp}.gkg.csv.zip"


def _fetch_bytes(url: str, timeout_s: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "casys-trader/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        data = response.read(MAX_CHUNK_BYTES + 1)
    if len(data) > MAX_CHUNK_BYTES:
        raise ValueError(f"chunk trop gros (>{MAX_CHUNK_BYTES} octets)")
    if not data:
        raise ValueError("chunk vide")
    return data


def _parse_tone(raw: str) -> float | None:
    try:
        return float(raw.split(",", 1)[0])
    except (ValueError, IndexError):
        return None


def _normalise_seendate(raw: str) -> str:
    digits = "".join(ch for ch in raw.strip() if ch.isdigit())
    if len(digits) >= 14:
        return f"{digits[0:8]}T{digits[8:14]}Z"
    return raw.strip()


def _parse_gkg(data: bytes) -> list[dict]:
    """Extrait les lignes (url, themes, tone) d'un export GKG. Jamais partiel."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [name for name in archive.namelist() if name.endswith(".csv")]
        if not members:
            raise ValueError("zip sans membre .csv")
        text = archive.read(members[0]).decode("utf-8", errors="replace")
    rows: list[dict] = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < _GKG_MIN_COLS:
            continue
        url = cols[_GKG_URL].strip()
        if not url:
            continue
        themes = [theme for theme in (part.strip() for part in cols[_GKG_THEMES].split(";")) if theme]
        padded = " " + " ".join(themes).lower().replace("_", " ") + " "
        if not any(f" {keyword} " in padded for keyword in THEME_KEYWORDS):
            continue
        hostname = (urlparse(url).hostname or "").removeprefix("www.")
        rows.append(
            {
                "url": url,
                "seendate": _normalise_seendate(cols[_GKG_DATE]),
                "domain": hostname or cols[_GKG_SOURCE].strip(),
                "themes": themes[:25],
                "tone": _parse_tone(cols[_GKG_TONE]),
            }
        )
    return rows


def _load_existing(events_path: Path) -> dict:
    if not events_path.exists():
        return {}
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    existing: dict = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("url"):
            existing[str(row["url"])] = row
    return existing


def collect_daily(
    state_dir: Path,
    now: datetime,
    *,
    get_bytes=None,
    log: logging.Logger | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """Télécharge les chunks GKG, filtre, dédup par URL, append. Fail-soft.

    Retourne {"collected": int, "skipped": int, "errors": int}.
    Idempotent : retraiter les mêmes chunks ne rajoute aucune ligne.
    """
    logger = log or logging.getLogger(__name__)
    fetch = get_bytes or (lambda url: _fetch_bytes(url, timeout_s))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ts_collected = now.astimezone(timezone.utc).isoformat()
    gdelt_dir = Path(state_dir) / GDELT_DIR
    gdelt_dir.mkdir(parents=True, exist_ok=True)
    events_path = gdelt_dir / EVENTS_FILE

    existing = _load_existing(events_path)
    collected = 0
    skipped = 0
    errors = 0
    for stamp in _chunk_stamps(now):
        try:
            rows = _parse_gkg(fetch(_chunk_url(stamp)))
        except Exception as exc:  # noqa: BLE001 — best-effort par chunk
            errors += 1
            logger.warning("gdelt: chunk %s ignoré : %s", stamp, str(exc)[:160])
            continue
        for row in rows:
            url = row["url"]
            if url in existing:
                skipped += 1
                continue
            if collected >= MAX_NEW_ROWS:
                skipped += 1
                continue
            existing[url] = {"ts_collected": ts_collected, "source": "gdelt_bulk", **row}
            collected += 1
    try:
        with events_path.open("w", encoding="utf-8") as handle:
            for row in existing.values():
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("gdelt: écriture events.jsonl impossible : %s", exc)
        return {"collected": 0, "skipped": skipped, "errors": errors + 1}
    return {"collected": collected, "skipped": skipped, "errors": errors}


_collect_lock = threading.Lock()
_collect_in_progress = False


def maybe_collect(
    state_dir: Path,
    now: datetime,
    *,
    force: bool = False,
    get_bytes=None,
    log: logging.Logger | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cooldown_h: float = DEFAULT_COOLDOWN_H,
) -> dict:
    """Déclenche collect_daily dans un thread daemon fire-and-forget si cooldown dépassé.

    Retourne immédiatement — le cycle n'est jamais bloqué.

    Le marqueur .last_collect est posé AU LANCEMENT du thread (convention
    partagée avec les autres collecteurs : anti-double-départ, pas une
    preuve de succès). Un verrou module garantit un seul thread à la fois.

    Retourne :
      {"triggered": False, "reason": "cooldown",     "elapsed_h": float}
      {"triggered": False, "reason": "in_progress"}
      {"triggered": True,  "_thread": threading.Thread}
    """
    global _collect_in_progress
    logger = log or logging.getLogger(__name__)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    gdelt_dir = Path(state_dir) / GDELT_DIR
    gdelt_dir.mkdir(parents=True, exist_ok=True)
    marker = gdelt_dir / MARKER_FILE
    if not force and marker.exists():
        try:
            last = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last = None
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed_h = (now.astimezone(timezone.utc) - last).total_seconds() / 3600.0
            if elapsed_h < cooldown_h:
                return {"triggered": False, "reason": "cooldown", "elapsed_h": elapsed_h}
    with _collect_lock:
        if _collect_in_progress:
            return {"triggered": False, "reason": "in_progress"}
        _collect_in_progress = True
    try:
        marker.write_text(now.astimezone(timezone.utc).isoformat(), encoding="utf-8")
    except OSError:
        pass

    def _run() -> None:
        global _collect_in_progress
        try:
            collect_daily(state_dir, now, get_bytes=get_bytes, log=logger, timeout_s=timeout_s)
        finally:
            with _collect_lock:
                _collect_in_progress = False

    thread = threading.Thread(target=_run, name="gdelt-collect", daemon=True)
    thread.start()
    return {"triggered": True, "_thread": thread}
