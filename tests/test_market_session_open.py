"""Anticipation de l'ouverture de session — alignement du réveil stale.

Invariant : on ne doit jamais dormir au-delà de la prochaine ouverture de marché
(sinon on rate la cloche après un weekend). DST géré via zoneinfo.

Repères DST 2026 : EDT (UTC-4) du 08/03 au 01/11 ; EST (UTC-5) sinon.
Donc 9h30 ET = 13h30 UTC l'été, 14h30 UTC l'hiver.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trader.tools.market import (
    clamp_wake_to_session_open,
    most_recent_session_open,
    next_regular_session_open,
)


def _utc(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


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
