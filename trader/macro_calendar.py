"""macro_calendar — calendrier des événements macro (P1a, spec §2).

Données structurées, aucun appel réseau dans le cycle daemon.
Le daemon lit state/macro_calendar.json (produit par un script offline) et
fusionne avec les constantes versionnées (FOMC 2026 connues un an à l'avance).

Convention de datation :
  Statement FOMC publié à 18:00Z le 2e jour de la réunion à 2 jours.
  Réunions à 1 jour (rares) : 18:00Z le jour unique.
  Source : https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm

Script offline (non dans le cycle daemon) :
  Scraper trimestriel → state/macro_calendar.json pour CPI/NFP/BCE/FOMC N+1.
  Format : [{"event": "FOMC", "at": "2026-07-29T18:00:00Z"}, ...]
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constante versionnée — FOMC 2026
# ---------------------------------------------------------------------------

# Les 8 réunions FOMC 2026 (dates publiées par la Fed un an à l'avance).
# Convention : statement à 18:00Z le 2e jour de la réunion à 2 jours.
# MAJ : bump la version de module + mettre à jour la constante chaque année.
# Scraping offline optionnel via script → state/macro_calendar.json.
FOMC_2026: tuple[dict, ...] = (
    {"event": "FOMC", "at": "2026-01-28T18:00:00Z"},  # 27-28 jan
    {"event": "FOMC", "at": "2026-03-18T18:00:00Z"},  # 17-18 mar
    {"event": "FOMC", "at": "2026-04-29T18:00:00Z"},  # 28-29 avr
    {"event": "FOMC", "at": "2026-06-17T18:00:00Z"},  # 16-17 jun
    {"event": "FOMC", "at": "2026-07-29T18:00:00Z"},  # 28-29 jul
    {"event": "FOMC", "at": "2026-09-16T18:00:00Z"},  # 15-16 sep
    {"event": "FOMC", "at": "2026-10-28T18:00:00Z"},  # 27-28 oct
    {"event": "FOMC", "at": "2026-12-09T18:00:00Z"},  # 8-9 dec
)

# Calendrier de référence codé en dur (seules les constantes FOMC 2026 pour l'instant).
# À enrichir avec CPI/NFP/BCE quand les dates sont connues (même mécanique).
DEFAULT_CALENDAR: tuple[dict, ...] = FOMC_2026

# ---------------------------------------------------------------------------
# Chargement et fusion
# ---------------------------------------------------------------------------


def load_calendar(path: str | Path) -> list[dict]:
    """Lit state/macro_calendar.json et fusionne avec DEFAULT_CALENDAR.

    Règles :
    - Le JSON prime pour les paires (event, at) identiques (dédup strict).
    - Les entrées DEFAULT_CALENDAR absentes du JSON sont ajoutées.
    - JSON absent, illisible ou corrompu → constantes seules, jamais d'exception.
    - Entrées JSON sans champ 'event' ou 'at' → ignorées silencieusement.

    Retourne une liste ordonnée : JSON d'abord, constantes manquantes ensuite.
    """
    json_entries: list[dict] = []
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "event" in item and "at" in item:
                    json_entries.append(
                        {"event": str(item["event"]), "at": str(item["at"])}
                    )
    except (OSError, ValueError, json.JSONDecodeError):
        # JSON absent / corrompu : on retombe sur DEFAULT_CALENDAR silencieusement.
        pass

    # Fusion : le JSON prime ; les constantes comblent les absences.
    seen: set[tuple[str, str]] = {(e["event"], e["at"]) for e in json_entries}
    merged: list[dict] = list(json_entries)
    for entry in DEFAULT_CALENDAR:
        key = (entry["event"], entry["at"])
        if key not in seen:
            merged.append({"event": entry["event"], "at": entry["at"]})
            seen.add(key)
    return merged


# ---------------------------------------------------------------------------
# Prochain événement macro
# ---------------------------------------------------------------------------


def macro_next(
    now: datetime,
    calendar: list[dict],
    limit: int = 3,
) -> list[dict]:
    """Retourne les `limit` prochains événements macro futurs par rapport à `now`.

    Chaque élément : {"event": str, "at": str (ISO 8601 Z), "in_h": float (1 déc.)}.
    Événements passés (at <= now) exclus.
    Entrées non parsables → ignorées silencieusement (best-effort, jamais d'exception).
    `now` sans tzinfo → traité comme UTC.
    """
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    futures: list[tuple[datetime, dict]] = []
    for entry in calendar:
        try:
            at_str = str(entry["at"])
            at = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at > now_utc:
            futures.append((at, entry))

    futures.sort(key=lambda x: x[0])
    result: list[dict] = []
    for at, entry in futures[:limit]:
        in_h = round((at - now_utc).total_seconds() / 3600.0, 1)
        result.append({"event": entry["event"], "at": entry["at"], "in_h": in_h})
    return result
