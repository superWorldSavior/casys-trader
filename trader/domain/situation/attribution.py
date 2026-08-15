"""Pure attribution of situation notes against market movement.

Judges the note, not the trader: a bullish family call is true or false
according to whether that family subsequently moved, even if no trade was taken.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from trader.domain.learnings.scoring import (
    SIGNIFICANT_RETURN_BAND,
    classify_decision_quality,
    compute_outcome_scores,
)
from trader.domain.market_data import Bar
from trader.domain.universe.selection_attribution import forward_return_over_sessions

DEFAULT_SHRINKAGE_K = 5.0
MAX_HORIZON_SESSIONS = 126

_MONTHS = {
    "janvier": 1,
    "jan": 1,
    "january": 1,
    "fevrier": 2,
    "feb": 2,
    "february": 2,
    "mars": 3,
    "mar": 3,
    "march": 3,
    "avril": 4,
    "apr": 4,
    "april": 4,
    "mai": 5,
    "may": 5,
    "juin": 6,
    "jun": 6,
    "june": 6,
    "juillet": 7,
    "jul": 7,
    "july": 7,
    "aout": 8,
    "aug": 8,
    "august": 8,
    "septembre": 9,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "octobre": 10,
    "oct": 10,
    "october": 10,
    "novembre": 11,
    "nov": 11,
    "november": 11,
    "decembre": 12,
    "dec": 12,
    "december": 12,
}

_UNIT_SESSIONS = {
    "d": 1,
    "j": 1,
    "w": 5,
    "s": 5,
    "m": 21,
    "q": 63,
    "t": 63,
}

_WORD_SESSIONS = {
    "jour": 1,
    "jours": 1,
    "seance": 1,
    "seances": 1,
    "day": 1,
    "days": 1,
    "semaine": 5,
    "semaines": 5,
    "week": 5,
    "weeks": 5,
    "mois": 21,
    "month": 21,
    "months": 21,
    "trimestre": 63,
    "trimestres": 63,
    "quarter": 63,
    "quarters": 63,
}

_QUALITATIVE_SESSIONS = {
    "immediat": 1,
    "now": 1,
    "today": 1,
    "intraday": 1,
    "seance": 1,
    "session": 1,
    "brief": 1,
    "preouverture": 1,
    "validite du brief": 1,
    "near": 1,
    "tres court terme": 2,
    "court terme": 5,
    "short": 5,
    "days": 5,
    "jours": 5,
    "court a moyen terme": 10,
    "court-moyen terme": 10,
    "court/moyen terme": 10,
    "court moyen terme": 10,
    "semaines": 10,
    "weeks": 10,
    "moyen terme": 21,
    "medium": 21,
    "mois": 21,
    "months": 21,
    "un mois": 21,
    "semaines a mois": 21,
    "semaines-mois": 21,
    "semaines a trimestres": 42,
    "trimestres": 63,
    "trimestre": 63,
    "quarters": 63,
    "quarter": 63,
    "long terme": 63,
    "moyen a long terme": 63,
    "moyen-long terme": 63,
}

_CONTAINS_SESSIONS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("intraday a quelques jours",), 5),
    (("seance a court terme",), 5),
    (("jours a semaines", "jours-semaines", "days-weeks", "days_weeks", "days weeks"), 10),
    (("seance a semaines", "seances a semaines"), 10),
    (("jours a mois", "jours-mois"), 21),
    (("semaines a mois", "semaines-mois"), 21),
    (("quelques mois",), 21),
)

_UNPARSEABLE_MARKERS = (
    "annee",
    "annees",
    "ans",
    "pluriannuel",
    "surveillance",
    "prochaine publication",
    "automne",
    "mois a annees",
    "mois-annees",
    "mois a 2027",
)

_RANGE_LETTER = re.compile(r"^(\d+)\s*[-–—]\s*(\d+)\s*([djwsmqt])s?$")
_NUM_LETTER = re.compile(r"^(\d+)\s*([djwsmqt])s?$")
_RANGE_WORD = re.compile(
    r"^(\d+)\s*[-–—]\s*(\d+)\s+"
    r"(jour|jours|seance|seances|day|days|semaine|semaines|week|weeks|"
    r"mois|month|months|trimestre|trimestres|quarter|quarters)$"
)
_NUM_WORD = re.compile(
    r"^(\d+)\s+"
    r"(jour|jours|seance|seances|day|days|semaine|semaines|week|weeks|"
    r"mois|month|months|trimestre|trimestres|quarter|quarters)$"
)
_NUM_A_NUM = re.compile(
    r"^(\d+)\s+a\s+(\d+)\s+"
    r"(jour|jours|seance|seances|day|days|semaine|semaines|week|weeks|"
    r"mois|month|months|trimestre|trimestres|quarter|quarters)$"
)
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
_YEAR_ONLY = re.compile(r"^20\d{2}$")
_DAY_MONTH = re.compile(
    r"^(?:jusqu(?:'| )?au\s+|fin\s+)?"
    r"(\d{1,2})\s+"
    r"(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|"
    r"novembre|decembre|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"(?:\s+(\d{4}))?$"
)
_MONTH_DAY_EN = re.compile(
    r"^(?:through\s+)?(?:fomc\s+)?"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[- ](\d{1,2})(?:\s+(\d{4}))?$"
)
_EVENT_DAY_MONTH = re.compile(
    r"(?:resultats du|seance du|jusqu(?:'| )?aux resultats du|jusqu(?:'| )?au fomc du|"
    r"jusqu(?:'| )?au)\s+"
    r"(\d{1,2})\s+"
    r"(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|"
    r"novembre|decembre|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"(?:\s+(\d{4}))?"
)
_FIN_MONTH = re.compile(
    r"^fin\s+"
    r"(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|"
    r"novembre|decembre)(?:\s+(\d{4}))?$"
)

__all__ = [
    "DEFAULT_SHRINKAGE_K",
    "MAX_HORIZON_SESSIONS",
    "classify_note_quality",
    "directional_action",
    "equal_weight_forward_return",
    "horizon_bucket",
    "parse_horizon_sessions",
    "parse_symbol_list",
    "score_note_outcomes",
    "targets_for_note",
    "to_flair_verdict",
]


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def _normalize_horizon(raw: object) -> str:
    text = _strip_accents(str(raw or "")).strip().lower()
    text = text.replace("'", "'").replace("'", "'").replace("`", "'")
    text = text.replace("_", " ").replace("/", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def _utc_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date()


def _weekdays_between(start: date, end: date) -> int | None:
    if end <= start:
        return None
    sessions = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            sessions += 1
    return sessions or None


def _clamp_sessions(value: int) -> int | None:
    if value <= 0:
        return None
    if value > MAX_HORIZON_SESSIONS:
        return None
    return value


def _sessions_from_unit(amount: int, unit: str) -> int | None:
    per = _UNIT_SESSIONS.get(unit)
    if per is None:
        return None
    return _clamp_sessions(amount * per)


def _sessions_from_word(amount: int, word: str) -> int | None:
    per = _WORD_SESSIONS.get(word)
    if per is None:
        return None
    return _clamp_sessions(amount * per)


def _resolve_calendar_date(
    day: int,
    month_token: str,
    year_token: str | None,
    as_of: date | None,
) -> date | None:
    month = _MONTHS.get(month_token)
    if month is None or day < 1 or day > 31:
        return None
    year = int(year_token) if year_token else (as_of.year if as_of is not None else None)
    if year is None:
        return None
    try:
        resolved = date(year, month, day)
    except ValueError:
        return None
    if as_of is not None and resolved < as_of and year_token is None:
        try:
            resolved = date(year + 1, month, day)
        except ValueError:
            return None
    return resolved


def _sessions_until(as_of: date | None, target: date | None) -> int | None:
    if as_of is None or target is None:
        return None
    return _clamp_sessions(_weekdays_between(as_of, target) or 0) if target > as_of else None


def parse_horizon_sessions(raw: object, as_of: object | None = None) -> int | None:
    """Map a free-text horizon onto a number of cash-session steps.

    Empty or unparseable values return ``None`` (caller marks ``non_evaluable``).
    Ranges keep the upper bound. Calendar targets count weekdays from ``as_of``.
    Horizons longer than ``MAX_HORIZON_SESSIONS`` are refused.
    """
    text = _normalize_horizon(raw)
    if not text:
        return None
    as_of_date = _utc_date(as_of)

    if text in _QUALITATIVE_SESSIONS:
        return _QUALITATIVE_SESSIONS[text]
    if _YEAR_ONLY.match(text):
        return None
    if any(marker in text for marker in _UNPARSEABLE_MARKERS):
        return None

    match = _RANGE_LETTER.match(text)
    if match:
        return _sessions_from_unit(int(match.group(2)), match.group(3))
    match = _NUM_LETTER.match(text)
    if match:
        return _sessions_from_unit(int(match.group(1)), match.group(2))
    match = _RANGE_WORD.match(text)
    if match:
        return _sessions_from_word(int(match.group(2)), match.group(3))
    match = _NUM_A_NUM.match(text)
    if match:
        return _sessions_from_word(int(match.group(2)), match.group(3))
    match = _NUM_WORD.match(text)
    if match:
        return _sessions_from_word(int(match.group(1)), match.group(2))

    match = _ISO_DATE.match(text)
    if match:
        try:
            target = date.fromisoformat(match.group(1))
        except ValueError:
            return None
        return _sessions_until(as_of_date, target)

    match = _DAY_MONTH.match(text)
    if match:
        target = _resolve_calendar_date(
            int(match.group(1)),
            match.group(2),
            match.group(3),
            as_of_date,
        )
        return _sessions_until(as_of_date, target)

    match = _MONTH_DAY_EN.match(text)
    if match:
        target = _resolve_calendar_date(
            int(match.group(2)),
            match.group(1),
            match.group(3),
            as_of_date,
        )
        return _sessions_until(as_of_date, target)

    match = _FIN_MONTH.match(text)
    if match:
        month = _MONTHS.get(match.group(1))
        year = int(match.group(2)) if match.group(2) else (as_of_date.year if as_of_date else None)
        if month is None or year is None:
            return None
        if month == 12:
            target = date(year, 12, 31)
        else:
            target = date(year, month + 1, 1) - timedelta(days=1)
        return _sessions_until(as_of_date, target)

    match = _EVENT_DAY_MONTH.search(text)
    if match:
        target = _resolve_calendar_date(
            int(match.group(1)),
            match.group(2),
            match.group(3),
            as_of_date,
        )
        return _sessions_until(as_of_date, target)

    for needles, sessions in _CONTAINS_SESSIONS:
        if any(needle in text for needle in needles):
            return sessions
    return None


def horizon_bucket(horizon_sessions: int | None) -> str:
    """Coarse label used by the analytics command."""
    if horizon_sessions is None or horizon_sessions <= 0:
        return "inconnu"
    if horizon_sessions <= 1:
        return "seance"
    if horizon_sessions <= 5:
        return "court"
    if horizon_sessions <= 10:
        return "semaines"
    if horizon_sessions <= 21:
        return "mois"
    if horizon_sessions <= 63:
        return "trimestre"
    return "long"


def directional_action(direction: object) -> str | None:
    """Translate a situation direction into the FLAIR action, or ``None``."""
    value = str(direction or "").strip().lower()
    if value in {"bullish", "risk_on"}:
        return "BUY"
    if value in {"bearish", "risk_off"}:
        return "SELL"
    return None


def parse_symbol_list(raw: object) -> tuple[str, ...]:
    """Parse a JSON list or sequence of tickers, dropping blanks."""
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        try:
            parsed = json.loads(str(raw or "[]"))
        except json.JSONDecodeError:
            return ()
        if not isinstance(parsed, list):
            return ()
        items = parsed
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        symbol = str(item or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return tuple(out)


def targets_for_note(
    note: Mapping[str, Any],
    *,
    families: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    """Resolve the market objects that judge this note.

    Family notes use the catalog basket. Symbol notes use ``section_name``.
    Alerts use the explicit ``symbols`` field. Zones are out of scope.
    """
    section_type = str(note.get("section_type") or "").strip()
    if section_type == "family":
        return tuple(str(symbol).strip() for symbol in families.get(str(note.get("section_name") or ""), ()) if str(symbol).strip())
    if section_type == "symbol":
        symbol = str(note.get("section_name") or "").strip()
        return (symbol,) if symbol else ()
    if section_type == "alert":
        return parse_symbol_list(note.get("symbols"))
    return ()


def equal_weight_forward_return(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    symbols: Sequence[str],
    as_of: str,
    horizon_sessions: int,
) -> tuple[float | None, int]:
    """Equal-weight close-to-close return of members with a complete path."""
    returns: list[float] = []
    for symbol in symbols:
        value = forward_return_over_sessions(
            bars_by_symbol.get(symbol, ()),
            as_of,
            horizon_sessions,
        )
        if value is not None:
            returns.append(value)
    if not returns:
        return None, 0
    return sum(returns) / len(returns), len(returns)


def classify_note_quality(
    direction: object,
    forward_return: float | None,
    *,
    band: float = SIGNIFICANT_RETURN_BAND,
) -> str:
    """Verdict of one note. Non-directional calls are ``non_evaluable``."""
    action = directional_action(direction)
    if action is None:
        return "non_evaluable"
    return classify_decision_quality(action, forward_return, band)


def to_flair_verdict(verdict: str) -> str:
    """Map a French quality verdict onto the WIN/LOSS vocabulary FLAIR expects."""
    if verdict == "gagnant":
        return "WIN"
    if verdict == "perdant":
        return "LOSS"
    return verdict


def score_note_outcomes(
    rows: Iterable[Mapping[str, Any]],
    *,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
) -> dict:
    """Run persistence-agnostic FLAIR on already-judged situation notes."""
    mapped = [
        {
            "id": int(row["id"]),
            "symbol": row.get("symbol") or "",
            "family": row.get("family") or "",
            "verdict": to_flair_verdict(str(row.get("verdict") or "")),
        }
        for row in rows
    ]
    return compute_outcome_scores(mapped, shrinkage_k=shrinkage_k)
