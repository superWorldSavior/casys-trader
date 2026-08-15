from __future__ import annotations

from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar
from trader.domain.situation.attribution import (
    classify_note_quality,
    directional_action,
    equal_weight_forward_return,
    parse_horizon_sessions,
    score_note_outcomes,
    targets_for_note,
)


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


def test_parse_horizon_formes_dominantes() -> None:
    as_of = "2026-08-10T16:00:00+00:00"
    assert parse_horizon_sessions("court terme") == 5
    assert parse_horizon_sessions("moyen terme") == 21
    assert parse_horizon_sessions("semaines") == 10
    assert parse_horizon_sessions("immédiat") == 1
    assert parse_horizon_sessions("medium") == 21
    assert parse_horizon_sessions("mois") == 21
    assert parse_horizon_sessions("trimestres") == 63
    assert parse_horizon_sessions("1 mois") == 21
    assert parse_horizon_sessions("weeks") == 10
    assert parse_horizon_sessions("short") == 5
    assert parse_horizon_sessions("séance") == 1
    assert parse_horizon_sessions("1-10d") == 10
    assert parse_horizon_sessions("1-3m") == 63
    assert parse_horizon_sessions("2 jours") == 2
    assert parse_horizon_sessions("11 jours") == 11
    assert parse_horizon_sessions("jusqu'au 21 août", as_of=as_of) == 9
    assert parse_horizon_sessions(None) is None
    assert parse_horizon_sessions("") is None
    assert parse_horizon_sessions("2027") is None
    assert parse_horizon_sessions("pluriannuel") is None
    assert parse_horizon_sessions("6 à 18 mois") is None


def test_direction_risk_on_off_sont_des_actions() -> None:
    assert directional_action("bullish") == "BUY"
    assert directional_action("risk_on") == "BUY"
    assert directional_action("bearish") == "SELL"
    assert directional_action("risk_off") == "SELL"
    assert directional_action("mixed") is None
    assert directional_action("neutral") is None


def test_note_bullish_qui_monte_est_gagnante() -> None:
    start = 100.0
    end = start * (1.0 + SIGNIFICANT_RETURN_BAND + 0.001)
    assert classify_note_quality("bullish", end / start - 1.0) == "gagnant"
    assert classify_note_quality("bearish", end / start - 1.0) == "perdant"
    assert classify_note_quality("mixed", 0.02) == "non_evaluable"
    assert classify_note_quality("bullish", None) == "non_evaluable"


def test_cible_famille_catalogue_zone_hors_perimetre() -> None:
    families = {"eu_industrials": ["AIR.PA", "SIE.DE"], "tw_memory": ["2408.TW"]}
    assert targets_for_note(
        {"section_type": "family", "section_name": "eu_industrials"},
        families=families,
    ) == ("AIR.PA", "SIE.DE")
    assert targets_for_note(
        {"section_type": "symbol", "section_name": "AIR.PA"},
        families=families,
    ) == ("AIR.PA",)
    assert targets_for_note(
        {"section_type": "alert", "symbols": '["AIR.PA", "SIE.DE"]'},
        families=families,
    ) == ("AIR.PA", "SIE.DE")
    assert targets_for_note(
        {"section_type": "alert", "symbols": "[]"},
        families=families,
    ) == ()
    assert targets_for_note(
        {"section_type": "zone", "section_name": "EU", "symbols": '["AIR.PA"]'},
        families=families,
    ) == ()
    assert targets_for_note(
        {"section_type": "family", "section_name": "tw_pcb_osat"},
        families=families,
    ) == ()


def test_equal_weight_ignore_les_membres_sans_chemin() -> None:
    start, end = 100.0, 110.0
    path = [_bar(f"2026-01-{day:02d}T16:00:00+00:00", start) for day in range(1, 6)]
    path.append(_bar("2026-01-06T16:00:00+00:00", end))
    forward, coverage = equal_weight_forward_return(
        {"AIR.PA": path, "SIE.DE": []},
        ["AIR.PA", "SIE.DE", "SU.PA"],
        "2026-01-01T08:00:00+00:00",
        5,
    )
    assert coverage == 1
    assert forward is not None
    assert abs(forward - 0.1) < 1e-12


def test_flair_famille_gagnante_au_dessus_de_famille_perdante() -> None:
    rows = [
        *({"id": index, "symbol": "eu_industrials", "family": "eu_industrials", "verdict": "gagnant"} for index in range(1, 4)),
        *({"id": index, "symbol": "tw_osat", "family": "tw_osat", "verdict": "perdant"} for index in range(4, 7)),
    ]
    result = score_note_outcomes(rows, shrinkage_k=5.0)
    alpha = [result["scores"][index] for index in (1, 2, 3)]
    beta = [result["scores"][index] for index in (4, 5, 6)]
    assert sum(alpha) / len(alpha) > sum(beta) / len(beta)
