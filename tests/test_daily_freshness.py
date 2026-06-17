"""Fraîcheur du daily basée sur la dernière séance COMPLÉTÉE (design §9, §13.0).

Un âge brut en minutes rejette à tort le daily du vendredi pendant le week-end
(timestamp yfinance ~minuit). On veut : frais tant que la dernière barre couvre
la dernière séance de bourse close pour la place du symbole.
"""

from datetime import datetime, timezone

from trader.tools.market import Bar, assess_daily_freshness, last_completed_session_date


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _daily(date_str: str):
    return [Bar(ts=date_str, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]


# Repères 2026 : 12/06 vendredi, 13 samedi, 14 dimanche, 15 lundi.


def test_daily_du_vendredi_reste_frais_le_weekend():
    # Le bug : âge brut 48h rejette le vendredi dès samedi minuit.
    fresh = assess_daily_freshness(_daily("2026-06-12"), now=_utc(2026, 6, 14, 18), symbol="AAPL")
    assert fresh.fresh is True


def test_lundi_matin_en_seance_le_daily_du_vendredi_suffit():
    # Lundi 10:00 EDT = 14:00 UTC, séance US ouverte (close pas atteint) → la
    # dernière séance complétée est vendredi ; son daily est encore frais.
    fresh = assess_daily_freshness(_daily("2026-06-12"), now=_utc(2026, 6, 15, 14), symbol="AAPL")
    assert fresh.fresh is True


def test_lundi_matin_un_daily_de_jeudi_est_stale():
    # Il manque la séance de vendredi → stale.
    fresh = assess_daily_freshness(_daily("2026-06-11"), now=_utc(2026, 6, 15, 14), symbol="AAPL")
    assert fresh.fresh is False
    assert fresh.reason == "stale_session"


def test_place_taipei_daily_du_vendredi_frais_le_samedi():
    # Le calendrier suit la place (TWSE), pas le défaut US.
    fresh = assess_daily_freshness(_daily("2026-06-12"), now=_utc(2026, 6, 13, 6), symbol="2330.TW")
    assert fresh.fresh is True


def test_pas_de_barres_est_stale_no_data():
    fresh = assess_daily_freshness([], now=_utc(2026, 6, 15, 14), symbol="AAPL")
    assert fresh.fresh is False
    assert fresh.reason == "no_data"


def test_last_completed_session_date_recule_au_vendredi_le_weekend():
    assert last_completed_session_date(_utc(2026, 6, 14, 18), symbol="AAPL").isoformat() == "2026-06-12"


def test_daily_timestampe_minuit_local_nest_pas_decale_par_le_fuseau():
    # yfinance peut étiqueter la barre daily à minuit LOCAL (+08:00 pour Taipei).
    # La date de séance est le 12 ; une conversion UTC la ferait reculer au 11 et
    # rejeterait à tort un daily valide. On compare la date du LABEL, pas l'instant UTC.
    bars = [Bar(ts="2026-06-12T00:00:00+08:00", open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]
    fresh = assess_daily_freshness(bars, now=_utc(2026, 6, 13, 6), symbol="2330.TW")  # samedi
    assert fresh.fresh is True
