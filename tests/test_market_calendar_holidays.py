"""Jours fériés + demi-séances via exchange_calendars — tests TDD.

Scénarios couverts :
- Euronext Paris fermé le 1er mai 2026 (Fête du Travail)
- Euronext Paris 14 juillet : IS une session (XPAR ne ferme pas le 14/07)
- NYSE fermé à Thanksgiving 2026 (26 nov)
- NYSE Black Friday 2026 (27 nov) : early close 13h00 ET = 18h00 UTC
- TWSE fermé pendant la semaine du Nouvel An lunaire 2026 (16-20 fév)
- next_regular_session_open saute les fériés (pas seulement les weekends)
- most_recent_session_open recule jusqu'à la dernière session réelle
- Bugs de bord (review Codex) :
  - most_recent_session_open doit être INCLUSIF à la minute d'ouverture (High #1)
  - last_completed_session_date doit être INCLUSIF à la minute de clôture (High #2)
"""

from __future__ import annotations

from datetime import datetime, timezone


from trader.market.market_data import (
    last_completed_session_date,
    most_recent_session_open,
    next_regular_session_open,
    session_snapshot,
)


def _utc(y, mo, d, h=12, mi=0, sec=0) -> datetime:
    return datetime(y, mo, d, h, mi, sec, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Euronext Paris — 1er mai 2026 (Fête du Travail)
# ---------------------------------------------------------------------------

def test_session_snapshot_paris_ferie_1er_mai_ferme():
    # 1er mai 2026 09h30 UTC = 11h30 Paris : serait "en séance" sans gestion des fériés
    snap = session_snapshot("HO.PA", now=_utc(2026, 5, 1, 9, 30))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


def test_next_open_paris_depuis_ferie_1er_mai():
    # Depuis le 1er mai 2026 (vendredi férié) → prochaine ouverture = lundi 4 mai 07h00 UTC
    nxt = next_regular_session_open(_utc(2026, 5, 1, 9), symbol="HO.PA")
    assert nxt == _utc(2026, 5, 4, 7, 0)


def test_most_recent_open_paris_avant_ferie_1er_mai():
    # Depuis le 1er mai 2026 12h UTC (pendant le férié) → dernière vraie ouverture = 30 avril 07h00 UTC
    prev = most_recent_session_open(_utc(2026, 5, 1, 12), symbol="HO.PA")
    assert prev == _utc(2026, 4, 30, 7, 0)


def test_session_snapshot_paris_14_juillet_ouvert():
    # 14 juillet 2026 09h30 UTC = 11h30 Paris : XPAR OUVRE le 14 juillet (jour ouvré)
    snap = session_snapshot("HO.PA", now=_utc(2026, 7, 14, 9, 30))
    assert snap["open"] is True


# ---------------------------------------------------------------------------
# NYSE — Thanksgiving 2026 (26 novembre, jeudi)
# ---------------------------------------------------------------------------

def test_session_snapshot_nyse_thanksgiving_ferme():
    # 26 nov 2026 17h00 UTC = 12h00 ET : serait "en séance" sans gestion des fériés
    snap = session_snapshot("SPY", now=_utc(2026, 11, 26, 17))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


def test_next_open_nyse_depuis_thanksgiving():
    # Depuis le 26 nov 2026 (jeudi férié) → prochaine ouverture = 27 nov (Black Friday)
    # NYSE est ouvert le Black Friday avec un early close
    nxt = next_regular_session_open(_utc(2026, 11, 26, 20), symbol="SPY")
    # 27 nov 2026 09h30 ET = 14h30 UTC
    assert nxt == _utc(2026, 11, 27, 14, 30)


# ---------------------------------------------------------------------------
# NYSE — Black Friday 2026 (27 novembre) — early close 13h00 ET = 18h00 UTC
# ---------------------------------------------------------------------------

def test_session_snapshot_nyse_black_friday_early_close():
    # 27 nov 2026 : NYSE ouvre à 09h30 ET mais ferme à 13h00 ET (early close)
    # À 17h00 UTC = 12h00 ET : en séance
    snap = session_snapshot("SPY", now=_utc(2026, 11, 27, 17))
    assert snap["open"] is True
    # La clôture anticipée doit être à 18h00 UTC (13h00 ET), pas 21h00 UTC (16h00 ET standard)
    assert snap["to_close_m"] == 60  # 60min avant 18h00 UTC


def test_session_snapshot_nyse_black_friday_apres_early_close():
    # À 18h30 UTC = 13h30 ET : marché déjà fermé (early close à 13h00 ET = 18h00 UTC)
    snap = session_snapshot("SPY", now=_utc(2026, 11, 27, 18, 30))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


# ---------------------------------------------------------------------------
# TWSE — Nouvel An lunaire 2026 (16-20 février fermé)
# ---------------------------------------------------------------------------

def test_session_snapshot_twse_nouel_an_lunaire_ferme():
    # 17 fév 2026 02h UTC = 10h Taipei : serait "en séance" sans gestion des fériés
    snap = session_snapshot("2330.TW", now=_utc(2026, 2, 17, 2))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


def test_next_open_twse_depuis_semaine_nouel_an_lunaire():
    # Depuis le 18 fév 2026 (mercredi férié) → prochaine ouverture = lundi 23 fév 01h00 UTC
    nxt = next_regular_session_open(_utc(2026, 2, 18, 4), symbol="2330.TW")
    assert nxt == _utc(2026, 2, 23, 1, 0)


def test_most_recent_open_twse_pendant_semaine_nouel_an_lunaire():
    # Depuis le 17 fév 2026 12h UTC → dernière vraie ouverture = mercredi 11 fév 01h00 UTC
    prev = most_recent_session_open(_utc(2026, 2, 17, 12), symbol="2330.TW")
    assert prev == _utc(2026, 2, 11, 1, 0)


# ---------------------------------------------------------------------------
# High #1 — most_recent_session_open INCLUSIF à l'instant d'ouverture
# (review Codex : previous_open() est strict → bug à la 1re minute)
# ---------------------------------------------------------------------------

def test_most_recent_open_nyse_pile_a_louverture():
    # NYSE Black Friday 2026 : ouverture à 09h30 ET = 14h30 UTC.
    # À l'instant EXACT d'ouverture, la session courante est la plus récente → retourner 14h30.
    # Bug avant fix : previous_open(14:30) retournait 2026-11-25T14:30Z (session précédente).
    result = most_recent_session_open(_utc(2026, 11, 27, 14, 30), symbol="SPY")
    assert result == _utc(2026, 11, 27, 14, 30)


def test_most_recent_open_nyse_1_min_apres_ouverture():
    # À 14h31 UTC (1 min après l'ouverture) : même réponse, session Black Friday.
    result = most_recent_session_open(_utc(2026, 11, 27, 14, 31), symbol="SPY")
    assert result == _utc(2026, 11, 27, 14, 30)


def test_most_recent_open_nyse_avant_ouverture_black_friday():
    # À 14h29 UTC (1 min AVANT l'ouverture Black Friday) → session précédente = mercredi 25 nov.
    result = most_recent_session_open(_utc(2026, 11, 27, 14, 29), symbol="SPY")
    assert result == _utc(2026, 11, 25, 14, 30)


def test_most_recent_open_nyse_pile_a_la_cloture_early():
    # À 18h00 UTC (early close Black Friday) : marché fermé, mais l'ouverture la plus récente
    # reste celle du 27 novembre (la session vient de se terminer).
    result = most_recent_session_open(_utc(2026, 11, 27, 18, 0), symbol="SPY")
    assert result == _utc(2026, 11, 27, 14, 30)


# ---------------------------------------------------------------------------
# High #2 — last_completed_session_date INCLUSIF à la minute de clôture
# (review Codex : previous_close() est strict → bug à la cloche)
# ---------------------------------------------------------------------------

def test_last_completed_session_nyse_black_friday_pile_a_la_cloture():
    # À 18h00 UTC (early close Black Friday 2026), la session du 27 novembre est COMPLÈTE.
    # Bug avant fix : previous_close(18:00) retournait 2026-11-25 → date incorrecte.
    result = last_completed_session_date(_utc(2026, 11, 27, 18, 0), symbol="SPY")
    assert result.isoformat() == "2026-11-27"


def test_last_completed_session_nyse_black_friday_apres_cloture():
    # À 18h01 UTC (1 min après l'early close) : même résultat, session du 27 nov complète.
    result = last_completed_session_date(_utc(2026, 11, 27, 18, 1), symbol="SPY")
    assert result.isoformat() == "2026-11-27"


def test_last_completed_session_nyse_black_friday_pendant():
    # À 17h59 UTC (1 min avant la clôture) : session en cours, non complétée.
    # → dernière complétée = mercredi 25 novembre.
    result = last_completed_session_date(_utc(2026, 11, 27, 17, 59), symbol="SPY")
    assert result.isoformat() == "2026-11-25"


def test_last_completed_session_nyse_standard_cloture():
    # Clôture standard NYSE : 21h00 UTC (16h00 ET). À 21h00 exactement → session du jour complète.
    result = last_completed_session_date(_utc(2026, 11, 25, 21, 0), symbol="SPY")
    assert result.isoformat() == "2026-11-25"


def test_last_completed_session_nyse_standard_avant_cloture():
    # À 20h59 UTC (1 min avant 21h00) : session en cours, non complétée → 24 nov.
    result = last_completed_session_date(_utc(2026, 11, 25, 20, 59), symbol="SPY")
    assert result.isoformat() == "2026-11-24"
