"""Anticipation de l'ouverture de session — alignement du réveil stale.

Invariant : on ne doit jamais dormir au-delà de la prochaine ouverture de marché
(sinon on rate la cloche après un weekend). DST géré via zoneinfo.

Repères DST 2026 : EDT (UTC-4) du 08/03 au 01/11 ; EST (UTC-5) sinon.
Donc 9h30 ET = 13h30 UTC l'été, 14h30 UTC l'hiver.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trader.tools.market import (
    _VENUE_BY_SUFFIX,
    _VENUE_BY_SYMBOL,
    clamp_wake_to_session_open,
    most_recent_session_open,
    next_regular_session_open,
    session_snapshot,
)
from trader.rotation_wiring import _EU_SUFFIXES, _EU_SYMBOLS


def _utc(y, mo, d, h, mi=0, sec=0) -> datetime:
    return datetime(y, mo, d, h, mi, sec, tzinfo=timezone.utc)


def test_vendredi_soir_saute_au_lundi_ete_edt():
    # Vendredi 12/06/2026 22h UTC → lundi 15/06 09h30 EDT = 13h30 UTC
    assert next_regular_session_open(_utc(2026, 6, 12, 22)) == _utc(2026, 6, 15, 13, 30)


def test_vendredi_soir_saute_au_lundi_hiver_est():
    # Vendredi 09/01/2026 22h UTC → lundi 12/01 09h30 EST = 14h30 UTC (DST géré)
    assert next_regular_session_open(_utc(2026, 1, 9, 22)) == _utc(2026, 1, 12, 14, 30)


def test_avant_ouverture_meme_jour():
    # Lundi 15/06 12h UTC (08h EDT, avant la cloche) → ouverture le jour même 13h30 UTC
    assert next_regular_session_open(_utc(2026, 6, 15, 12)) == _utc(2026, 6, 15, 13, 30)


def test_apres_ouverture_passe_au_lendemain():
    # Lundi 15/06 18h UTC (14h EDT, après la cloche) → mardi 16/06 13h30 UTC
    assert next_regular_session_open(_utc(2026, 6, 15, 18)) == _utc(2026, 6, 16, 13, 30)


def test_clamp_loin_de_louverture_laisse_le_backoff_intact():
    # Vendredi 23h UTC, open lundi : 120min de backoff ne franchit pas l'open → inchangé
    assert clamp_wake_to_session_open(120.0, now=_utc(2026, 6, 12, 23)) == 120.0


def test_clamp_pres_de_louverture_raccourcit_avant_la_cloche():
    # Lundi 13h UTC (open 13h30), wake stale 120min → réveil 5min avant = 25min
    assert clamp_wake_to_session_open(120.0, now=_utc(2026, 6, 15, 13)) == 25.0


def test_clamp_plancher_jamais_zero_ni_negatif():
    # Lundi 13h29 UTC : la cible (13h25) est déjà passée → plancher à 1min, pas <= 0
    assert clamp_wake_to_session_open(120.0, now=_utc(2026, 6, 15, 13, 29)) == 1.0


def test_clamp_wake_non_positif_planche_aussi():
    # Entrée pathologique (CLI mal validé) : wake=0 ou négatif, loin de l'open →
    # le plancher s'applique sur les DEUX chemins, jamais de réveil immédiat (P1-a).
    assert clamp_wake_to_session_open(0.0, now=_utc(2026, 6, 12, 23)) == 1.0
    assert clamp_wake_to_session_open(-5.0, now=_utc(2026, 6, 12, 23)) == 1.0


def test_bascule_dst_printemps_saute_correctement():
    # Vendredi 06/03/2026 22h UTC, juste avant le passage EST→EDT (dim 08/03) →
    # lundi 09/03 déjà en EDT : 09h30 EDT = 13h30 UTC.
    assert next_regular_session_open(_utc(2026, 3, 6, 22)) == _utc(2026, 3, 9, 13, 30)


def test_bascule_dst_automne_saute_correctement():
    # Vendredi 30/10/2026 22h UTC, juste avant le passage EDT→EST (dim 01/11) →
    # lundi 02/11 déjà en EST : 09h30 EST = 14h30 UTC.
    assert next_regular_session_open(_utc(2026, 10, 30, 22)) == _utc(2026, 11, 2, 14, 30)


def test_open_exact_passe_au_lendemain():
    # À 13h30 UTC pile (9h30 EDT, la cloche) l'ouverture du jour n'est plus
    # STRICTEMENT > now → on retourne celle du lendemain (contrat assumé).
    assert next_regular_session_open(_utc(2026, 6, 15, 13, 30)) == _utc(2026, 6, 16, 13, 30)


def test_grace_a_la_cloche_pile_poll_serre():
    # 13h30 UTC pile : dans la fenêtre de grâce → poll serré 1min (pas de saut
    # vers demain qui ferait dormir tout le backoff). C'est le fix P1-b.
    assert clamp_wake_to_session_open(120.0, now=_utc(2026, 6, 15, 13, 30)) == 1.0


def test_grace_juste_apres_la_cloche_poll_serre():
    # 13h35 UTC (5min après l'open EDT), data encore stale → poll serré.
    assert clamp_wake_to_session_open(120.0, now=_utc(2026, 6, 15, 13, 35)) == 1.0


def test_grace_borne_a_la_fin_de_fenetre():
    # 13h50 UTC = open+20min pile : encore dans la grâce (borne incluse).
    assert clamp_wake_to_session_open(120.0, now=_utc(2026, 6, 15, 13, 50)) == 1.0


def test_apres_grace_retombe_sur_prochaine_ouverture():
    # 13h51 UTC : grâce expirée, prochaine ouverture = demain (loin) → backoff
    # intact (120min), on ne poll plus serré pour rien.
    assert clamp_wake_to_session_open(120.0, now=_utc(2026, 6, 15, 13, 51)) == 120.0


def test_most_recent_session_open_recule_au_vendredi_le_weekend():
    # Samedi 14/06 12h UTC → dernière ouverture = vendredi 12/06 13h30 UTC (EDT).
    assert most_recent_session_open(_utc(2026, 6, 13, 12)) == _utc(2026, 6, 12, 13, 30)


# --- Calendrier par place de cotation (symbol=) -------------------------------
# Le réveil stale doit viser l'ouverture du marché DU symbole, pas Wall Street :
# TWSE (.TW) ouvre 09h00 Asia/Taipei = 01h00 UTC (pas de DST à Taïwan),
# Euronext Paris (.PA, ^FCHI) et XETRA (.DE) ouvrent 09h00 locale = 07h00 UTC
# l'été (CEST). Sans symbol= (ou symbole inconnu) : comportement US inchangé.


def test_next_open_twse_meme_jour():
    # Mardi 10/06 00h UTC → ouverture TWSE le jour même 01h00 UTC.
    assert next_regular_session_open(
        _utc(2026, 6, 10, 0), symbol="2330.TW"
    ) == _utc(2026, 6, 10, 1, 0)


def test_next_open_twse_vendredi_soir_saute_au_lundi():
    # Vendredi 12/06 23h UTC → lundi 15/06 09h00 Taipei = 01h00 UTC.
    assert next_regular_session_open(
        _utc(2026, 6, 12, 23), symbol="2454.TW"
    ) == _utc(2026, 6, 15, 1, 0)


def test_next_open_euronext_paris_ete():
    # Lundi 15/06 05h UTC → 09h00 Europe/Paris (CEST) = 07h00 UTC.
    assert next_regular_session_open(
        _utc(2026, 6, 15, 5), symbol="HO.PA"
    ) == _utc(2026, 6, 15, 7, 0)


def test_next_open_fchi_suit_paris():
    # ^FCHI est coté à Paris : même calendrier que les .PA.
    assert next_regular_session_open(
        _utc(2026, 6, 15, 5), symbol="^FCHI"
    ) == _utc(2026, 6, 15, 7, 0)


def test_next_open_xetra_ete():
    # Lundi 15/06 05h UTC → 09h00 Europe/Berlin (CEST) = 07h00 UTC.
    assert next_regular_session_open(
        _utc(2026, 6, 15, 5), symbol="RHM.DE"
    ) == _utc(2026, 6, 15, 7, 0)


def test_next_open_euronext_hiver_cet():
    # Vendredi 09/01/2026 23h UTC → lundi 12/01 09h00 CET (UTC+1) = 08h00 UTC.
    assert next_regular_session_open(
        _utc(2026, 1, 9, 23), symbol="AM.PA"
    ) == _utc(2026, 1, 12, 8, 0)


def test_next_open_symbole_inconnu_garde_le_defaut_us():
    # SPY (et tout symbole non mappé) : calendrier US inchangé.
    assert next_regular_session_open(
        _utc(2026, 6, 15, 12), symbol="SPY"
    ) == _utc(2026, 6, 15, 13, 30)


def test_clamp_twse_pres_de_louverture_raccourcit():
    # Lundi 15/06 00h40 UTC, open TWSE 01h00 : wake 120min → 5min avant = 15min.
    assert clamp_wake_to_session_open(
        120.0, now=_utc(2026, 6, 15, 0, 40), symbol="2330.TW"
    ) == 15.0


def test_clamp_twse_grace_apres_la_cloche_poll_serre():
    # 01h05 UTC (5min après l'open TWSE), data encore stale → poll serré 1min.
    assert clamp_wake_to_session_open(
        120.0, now=_utc(2026, 6, 15, 1, 5), symbol="2330.TW"
    ) == 1.0


def test_clamp_paris_pres_de_louverture_raccourcit():
    # Lundi 15/06 06h30 UTC, open Paris 07h00 : wake 120min → 5min avant = 25min.
    assert clamp_wake_to_session_open(
        120.0, now=_utc(2026, 6, 15, 6, 30), symbol="HO.PA"
    ) == 25.0


def test_most_recent_open_twse_weekend_recule_au_vendredi():
    # Samedi 13/06 12h UTC → dernière ouverture TWSE = vendredi 12/06 01h00 UTC.
    assert most_recent_session_open(
        _utc(2026, 6, 13, 12), symbol="2330.TW"
    ) == _utc(2026, 6, 12, 1, 0)


# --- session_snapshot : où en est la session du symbole (fait injecté au LLM) --
# Le code répond à « le marché du symbole est-il ouvert, depuis/pour combien de
# temps ? » pour que le prompt n'ait pas à lister les horaires des places.


def test_session_snapshot_us_en_seance():
    # Lundi 15/06 14h30 UTC = 10h30 EDT (open 09h30, close 16h00) : en séance,
    # 60min depuis l'open, 330min avant la cloche de clôture.
    snap = session_snapshot("SPY", now=_utc(2026, 6, 15, 14, 30))
    assert snap == {"open": True, "since_open_m": 60, "to_close_m": 330}


def test_session_snapshot_us_hors_seance_avant_ouverture():
    # Lundi 15/06 12h00 UTC = 08h00 EDT : Wall Street n'est pas encore ouverte.
    snap = session_snapshot("NVDA", now=_utc(2026, 6, 15, 12, 0))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


def test_session_snapshot_twse_en_seance():
    # Lundi 15/06 02h00 UTC = 10h00 Taipei (open 09h00, close 13h30) : en séance.
    snap = session_snapshot("2330.TW", now=_utc(2026, 6, 15, 2, 0))
    assert snap == {"open": True, "since_open_m": 60, "to_close_m": 210}


def test_session_snapshot_twse_fermee_l_apres_midi_utc():
    # Lundi 15/06 14h UTC = 22h00 Taipei : TWSE fermée.
    snap = session_snapshot("2330.TW", now=_utc(2026, 6, 15, 14, 0))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


def test_session_snapshot_paris_en_seance():
    # Lundi 15/06 08h00 UTC = 10h00 Paris (open 09h00, close 17h30) : en séance.
    snap = session_snapshot("HO.PA", now=_utc(2026, 6, 15, 8, 0))
    assert snap == {"open": True, "since_open_m": 60, "to_close_m": 450}


def test_session_snapshot_amsterdam_en_seance_matin_eu():
    # Lundi 15/06 07h21:39 UTC = 09h21 Amsterdam : Euronext Amsterdam est ouvert.
    snap = session_snapshot("ASML.AS", now=_utc(2026, 6, 15, 7, 21, 39))
    assert snap["open"] is True


def test_session_snapshot_londres_en_seance_avant_ouverture_us():
    # Lundi 15/06 07h30 UTC = 08h30 Londres, mais 03h30 EDT : le fallback US
    # serait fermé, la place LSE doit être ouverte.
    snap = session_snapshot("AZN.L", now=_utc(2026, 6, 15, 7, 30))
    assert snap == {"open": True, "since_open_m": 30, "to_close_m": 480}


def test_session_snapshot_helsinki_en_seance_a_l_heure_locale():
    # Lundi 15/06 07h30 UTC = 10h30 Helsinki (open 10h00 EEST) : en séance.
    snap = session_snapshot("NOKIA.HE", now=_utc(2026, 6, 15, 7, 30))
    assert snap == {"open": True, "since_open_m": 30, "to_close_m": 480}


def test_session_snapshot_eu_weekend_fermee():
    # Samedi 13/06 07h30 UTC serait dans la plage Amsterdam un jour ouvré.
    snap = session_snapshot("ASML.AS", now=_utc(2026, 6, 13, 7, 30))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


def test_session_snapshot_weekend_fermee():
    # Samedi 13/06 : tout est fermé, même en heures « de séance ».
    snap = session_snapshot("SPY", now=_utc(2026, 6, 13, 14, 30))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


def test_session_snapshot_fx_toujours_ouvert_en_semaine():
    # FX ~24h/5 : pas de calendrier de place — open=True en semaine, sans bornes.
    snap = session_snapshot("EURUSD=X", now=_utc(2026, 6, 15, 3, 0))
    assert snap == {"open": True, "since_open_m": None, "to_close_m": None}


def test_session_snapshot_fx_ferme_le_weekend():
    snap = session_snapshot("EURUSD=X", now=_utc(2026, 6, 13, 12, 0))
    assert snap == {"open": False, "since_open_m": None, "to_close_m": None}


def test_market_session_mapping_couvre_tous_les_suffixes_eu_du_pool():
    missing_suffixes = sorted(set(_EU_SUFFIXES) - set(_VENUE_BY_SUFFIX))
    missing_symbols = sorted(
        symbol
        for symbol in _EU_SYMBOLS
        if symbol not in _VENUE_BY_SYMBOL
        and not any(symbol.endswith(suffix) for suffix in _VENUE_BY_SUFFIX)
    )
    assert missing_suffixes == []
    assert missing_symbols == []


def test_session_snapshot_two_suit_taipei_pas_le_defaut_us():
    # Un ticker TPEx (.TWO) doit suivre le calendrier Taïwan (09:00-13:30 Asia/Taipei),
    # pas le défaut US. Les deux assertions sont DISCRIMINANTES : elles échoueraient si
    # .TWO retombait sur America/New_York.
    # 02:00 UTC = 10:00 Taipei (TW ouvert) MAIS 22:00 EDT la veille (US fermé).
    assert session_snapshot("6488.TWO", now=_utc(2026, 6, 10, 2))["open"] is True
    # 14:00 UTC = 22:00 Taipei (TW fermé) MAIS 10:00 EDT (US ouvert).
    assert session_snapshot("6488.TWO", now=_utc(2026, 6, 10, 14))["open"] is False
