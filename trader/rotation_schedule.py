"""Gate de déclenchement de la rotation par clôture de session de marché.

Le temps est TOUJOURS passé en argument (ISO 8601 UTC) — jamais d'horloge interne.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

_DEFAULT_SESSIONS: dict[str, str] = {
    "TW": "05:30",
    "EU": "15:30",
    "US": "20:00",
}


def load_sessions(config_dir: str) -> dict[str, str]:
    """Lit config/sessions.yaml (venue → "HH:MM").

    Retourne le défaut si le fichier est absent ou illisible.
    """
    path = os.path.join(config_dir, "config", "sessions.yaml")
    try:
        import yaml  # type: ignore[import-untyped]

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return dict(_DEFAULT_SESSIONS)
    except Exception:
        return dict(_DEFAULT_SESSIONS)


def _close_dt(date: datetime, hhmm: str) -> datetime:
    """Construit un datetime UTC correspondant à date.date() à HH:MM."""
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return datetime(date.year, date.month, date.day, h, m, tzinfo=timezone.utc)


def closed_sessions_since(
    now_iso: str,
    last_rotation_iso: str | None,
    sessions: dict[str, str],
) -> list[str]:
    """Venues dont la clôture est tombée dans (last_rotation, now].

    - last_rotation_iso=None/"" → bootstrap : toutes les venues dont la clôture
      du jour de now est déjà passée (≤ now).
    - Gère le passage de minuit : si now > last_rotation et que des clôtures de
      la veille (ou avant-hier etc.) n'ont pas encore été couvertes, elles comptent.
    - Résultat trié alphabétiquement.
    - Temps toujours passé en argument — pas de datetime.now().
    """
    now = datetime.fromisoformat(now_iso).astimezone(timezone.utc)
    bootstrap = not last_rotation_iso  # None ou ""

    if bootstrap:
        # Toutes les clôtures du jour de now déjà passées (≤ now)
        hits: list[str] = []
        for venue, hhmm in sessions.items():
            close = _close_dt(now, hhmm)
            if close <= now:
                hits.append(venue)
        return sorted(hits)

    last = datetime.fromisoformat(last_rotation_iso).astimezone(timezone.utc)

    hits = []
    # Itère sur chaque jour entier entre last (inclus) et now (inclus)
    # pour trouver toutes les clôtures tombées dans (last, now]
    current_date = last.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Nombre de jours à scanner (≥1 : le jour de last, jusqu'au jour de now)
    day = current_date
    while day <= end_date:
        for venue, hhmm in sessions.items():
            close = _close_dt(day, hhmm)
            if last < close <= now:
                if venue not in hits:
                    hits.append(venue)
        day += timedelta(days=1)

    return sorted(hits)


def rotation_due(
    now_iso: str,
    last_rotation_iso: str | None,
    sessions: dict[str, str],
) -> bool:
    """Retourne True si au moins une session a clôturé depuis last_rotation."""
    return bool(closed_sessions_since(now_iso, last_rotation_iso, sessions))
