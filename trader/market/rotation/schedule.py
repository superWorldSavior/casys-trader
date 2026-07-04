"""Gate de déclenchement de la rotation par clôture de session de marché.

Le temps est TOUJOURS passé en argument (ISO 8601 UTC) — jamais d'horloge interne.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

SessionHours = dict[str, str]

_DEFAULT_SESSIONS: dict[str, SessionHours] = {
    "TW": {"open": "01:00", "close": "05:30"},
    "EU": {"open": "07:00", "close": "15:30"},
    "US": {"open": "13:30", "close": "20:00"},
}


def load_sessions(config_dir: str) -> dict[str, SessionHours]:
    """Lit config/sessions.yaml (venue → {"open": "HH:MM", "close": "HH:MM"}).

    Retourne le défaut si le fichier est absent ou illisible.
    """
    path = os.path.join(config_dir, "config", "sessions.yaml")
    try:
        import yaml  # type: ignore[import-untyped]

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            sessions: dict[str, SessionHours] = {}
            for venue, hours in data.items():
                if not isinstance(hours, dict):
                    return dict(_DEFAULT_SESSIONS)
                open_hhmm = hours.get("open")
                close_hhmm = hours.get("close")
                if open_hhmm is None or close_hhmm is None:
                    return dict(_DEFAULT_SESSIONS)
                sessions[str(venue)] = {
                    "open": str(open_hhmm),
                    "close": str(close_hhmm),
                }
            return sessions
        return dict(_DEFAULT_SESSIONS)
    except Exception:
        return dict(_DEFAULT_SESSIONS)


def _close_dt(date: datetime, hhmm: str) -> datetime:
    """Construit un datetime UTC correspondant à date.date() à HH:MM."""
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return datetime(date.year, date.month, date.day, h, m, tzinfo=timezone.utc)


def open_venues(now_iso: str, sessions: dict[str, SessionHours]) -> list[str]:
    """Retourne les venues ouvertes à now_iso, triées pour déterminisme."""
    now = datetime.fromisoformat(now_iso).astimezone(timezone.utc)
    if now.weekday() >= 5:
        return []

    hits = ["FX"]
    for venue, hours in sessions.items():
        open_dt = _close_dt(now, hours["open"])
        close_dt = _close_dt(now, hours["close"])
        if open_dt <= now < close_dt:
            hits.append(venue)

    return sorted(hits)


def preopen_venues(
    now_iso: str, sessions: dict[str, SessionHours], *, window_minutes: int = 90
) -> list[str]:
    """Venues actions dont la PROCHAINE ouverture est dans (now, now+window].

    Pré-open = analysable pour préparer le gong, PAS exécutable (brique 3 gate).
    N'inclut jamais FX (24/5, pas de gong). Une venue déjà ouverte n'est pas pré-open.
    Calendrier v1 = HH:MM + jour ouvré, cohérent avec open_venues (cf Task A4 pour
    l'alignement exchange_calendars).
    """
    now = datetime.fromisoformat(now_iso).astimezone(timezone.utc)
    horizon = now + timedelta(minutes=window_minutes)
    open_now = set(open_venues(now_iso, sessions))
    hits: list[str] = []
    for venue, hours in sessions.items():
        # FX = 24/5, pas de gong → jamais pré-open. Garantie PAR CONSTRUCTION
        # (ne pas dépendre de la présence de FX dans open_now, faux le week-end).
        if venue == "FX" or venue in open_now:
            continue
        # prochaine ouverture : aujourd'hui si pas encore passée, sinon prochain jour ouvré
        candidate = _close_dt(now, hours["open"])
        for _ in range(4):  # aujourd'hui + jusqu'à 3 jours (saute le week-end)
            if candidate > now and candidate.weekday() < 5:
                break
            candidate = _close_dt(candidate + timedelta(days=1), hours["open"])
        if now < candidate <= horizon:
            hits.append(venue)
    return sorted(hits)


def analyzable_venues(
    now_iso: str, sessions: dict[str, SessionHours], *, preopen_window_minutes: int = 90
) -> list[str]:
    """Venues à inclure dans l'univers de SURVEILLANCE = ouvertes ∪ pré-open.

    L'exécutabilité reste décidée au runtime par classify_symbol_context (brique 2/3) ;
    cette union ne dit que « surveille/analyse », jamais « trade ».
    """
    union = set(open_venues(now_iso, sessions))
    union |= set(preopen_venues(now_iso, sessions, window_minutes=preopen_window_minutes))
    return sorted(union)


def closed_sessions_since(
    now_iso: str,
    last_rotation_iso: str | None,
    sessions: dict[str, SessionHours],
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
        for venue, hours in sessions.items():
            close = _close_dt(now, hours["close"])
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
        for venue, hours in sessions.items():
            close = _close_dt(day, hours["close"])
            if last < close <= now:
                if venue not in hits:
                    hits.append(venue)
        day += timedelta(days=1)

    return sorted(hits)


def rotation_due(
    now_iso: str,
    last_rotation_iso: str | None,
    sessions: dict[str, SessionHours],
) -> bool:
    """Retourne True si au moins une session a clôturé depuis last_rotation."""
    return bool(closed_sessions_since(now_iso, last_rotation_iso, sessions))
