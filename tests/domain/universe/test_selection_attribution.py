from __future__ import annotations

from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar
from trader.domain.universe.selection_attribution import (
    DEFAULT_FORWARD_SESSIONS,
    FLAIR_GROUP_ALLOCATION,
    FLAIR_GROUP_DIRECTION,
    FLAIR_GROUP_DIRECTION_VIEW,
    MIN_BENCH_EVALUATED,
    classify_allocation_quality,
    classify_selection_quality,
    direction_claim,
    directional_action,
    directional_view_action,
    flair_scoring_group,
    forward_return_over_sessions,
    has_as_of_session,
    is_live_feedback_row,
    opportunity,
    score_selection_outcomes,
)


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


def _session_bars(start_close: float, end_close: float, *, sessions: int = DEFAULT_FORWARD_SESSIONS) -> list[Bar]:
    bars = [_bar(f"2026-01-{day:02d}T16:00:00+00:00", start_close) for day in range(1, sessions + 1)]
    bars.append(_bar(f"2026-01-{sessions + 1:02d}T16:00:00+00:00", end_close))
    return bars


def test_long_qui_monte_au_dela_de_la_bande_est_gagnant() -> None:
    start = 100.0
    end = start * (1.0 + SIGNIFICANT_RETURN_BAND + 0.001)
    bars = _session_bars(start, end)
    forward = forward_return_over_sessions(bars, "2026-01-01T08:00:00+00:00", DEFAULT_FORWARD_SESSIONS)
    assert forward is not None and forward > SIGNIFICANT_RETURN_BAND
    assert classify_selection_quality(["long"], forward) == "gagnant"


def test_long_qui_baisse_est_perdant() -> None:
    start = 100.0
    end = start * (1.0 - SIGNIFICANT_RETURN_BAND - 0.001)
    bars = _session_bars(start, end)
    forward = forward_return_over_sessions(bars, "2026-01-01T08:00:00+00:00", DEFAULT_FORWARD_SESSIONS)
    assert classify_selection_quality(["long"], forward) == "perdant"


def test_allowed_sides_vide_ou_bidirectionnel_non_evaluable() -> None:
    assert classify_selection_quality([], 0.02) == "non_evaluable"
    assert classify_selection_quality(("long", "short"), 0.02) == "non_evaluable"
    assert classify_selection_quality(["short", "long"], -0.03) == "non_evaluable"
    assert directional_action(["long"]) == "BUY"
    assert directional_action(["short"]) == "SELL"
    assert directional_action(["long", "short"]) is None


def test_vue_long_bias_juge_buy_si_sides_ouverts() -> None:
    assert directional_view_action("long_bias") == "BUY"
    assert directional_view_action("short_bias") == "SELL"
    assert directional_view_action("two_sided") is None
    assert directional_view_action("neutral") is None
    assert directional_view_action("") is None
    assert classify_selection_quality(
        ("long", "short"), 0.02, directional_view="long_bias"
    ) == "gagnant"
    assert classify_selection_quality(
        ("long", "short"), -0.02, directional_view="long_bias"
    ) == "perdant"
    assert classify_selection_quality(
        ("long", "short"), -0.02, directional_view="short_bias"
    ) == "gagnant"
    assert classify_selection_quality(
        ("long", "short"), 0.02, directional_view="two_sided"
    ) == "non_evaluable"


def test_sides_durs_prioritaires_sur_la_vue() -> None:
    assert direction_claim(("long",), "short_bias") == ("BUY", "allowed_sides")
    assert direction_claim(("long", "short"), "long_bias") == ("BUY", "directional_view")
    assert direction_claim(("long", "short"), "two_sided") is None
    assert classify_selection_quality(["long"], -0.02, directional_view="short_bias") == "perdant"


def test_flair_lift_famille_gagnante_superieur_a_famille_perdante() -> None:
    rows = [
        *({"id": index, "symbol": f"WIN{index}", "family": "alpha", "verdict": "gagnant"} for index in range(1, 4)),
        *({"id": index, "symbol": f"LOSS{index}", "family": "beta", "verdict": "perdant"} for index in range(4, 7)),
    ]
    result = score_selection_outcomes(rows, shrinkage_k=5.0)
    alpha = [result["scores"][index] for index in (1, 2, 3)]
    beta = [result["scores"][index] for index in (4, 5, 6)]
    assert sum(alpha) / len(alpha) > sum(beta) / len(beta)


def _bench(value: float, *, n: int = MIN_BENCH_EVALUATED) -> list[float]:
    return [value] * n


def test_opportunity_est_la_valeur_absolue_ou_none() -> None:
    assert opportunity(0.02) == 0.02
    assert opportunity(-0.03) == 0.03
    assert opportunity(0.0) == 0.0
    assert opportunity(None) is None


def test_allocation_gagne_si_exces_au_dela_de_la_bande() -> None:
    median = 0.02
    pick = median + SIGNIFICANT_RETURN_BAND + 0.001
    assert classify_allocation_quality(pick, _bench(median)) == "gagnant"


def test_allocation_perd_si_exces_sous_la_bande() -> None:
    median = 0.02
    pick = median - SIGNIFICANT_RETURN_BAND - 0.001
    assert classify_allocation_quality(pick, _bench(median)) == "perdant"


def test_allocation_neutre_quand_exces_dans_la_bande() -> None:
    median = 0.02
    assert classify_allocation_quality(median, _bench(median)) == "neutre"
    assert classify_allocation_quality(median + SIGNIFICANT_RETURN_BAND * 0.5, _bench(median)) == "neutre"
    assert classify_allocation_quality(median - SIGNIFICANT_RETURN_BAND * 0.5, _bench(median)) == "neutre"


def test_banc_vide_ou_maigre_est_non_evaluable() -> None:
    assert classify_allocation_quality(0.04, []) == "non_evaluable"
    assert classify_allocation_quality(0.04, _bench(0.01, n=MIN_BENCH_EVALUATED - 1)) == "non_evaluable"
    assert classify_allocation_quality(0.04, _bench(0.01, n=MIN_BENCH_EVALUATED)) == "gagnant"


def test_membre_du_banc_sans_barres_exclu_jamais_compte_zero() -> None:
    # 7 vraies opportunités nulles + 1 membre manquant. Compter le manquant
    # comme 0 donnerait n=8 et un verdict ; l'exclure laisse le banc maigre.
    bench: list[float | None] = [0.0] * (MIN_BENCH_EVALUATED - 1) + [None]
    assert classify_allocation_quality(0.10, bench) == "non_evaluable"


def test_pick_sans_barres_est_non_evaluable() -> None:
    assert classify_allocation_quality(None, _bench(0.02)) == "non_evaluable"
    assert classify_allocation_quality(opportunity(None), _bench(0.02)) == "non_evaluable"


def test_horizon_immature_pas_de_verdict_allocation() -> None:
    bars = [_bar("2026-01-01T16:00:00+00:00", 100.0)]
    as_of = "2026-01-01T08:00:00+00:00"
    forward = forward_return_over_sessions(bars, as_of, DEFAULT_FORWARD_SESSIONS)
    assert forward is None
    assert has_as_of_session(bars, as_of)
    assert opportunity(forward) is None
    # Pending: ancre présente, horizon pas échu. Ce n'est pas un 0 d'opportunité.
    assert classify_allocation_quality(0.0, _bench(0.02)) == "perdant"


def test_pick_sans_aucune_barre_n_a_pas_d_ancre() -> None:
    assert not has_as_of_session([], "2026-01-01T08:00:00+00:00")
    assert not has_as_of_session(
        [_bar("not-a-timestamp", 100.0)],
        "2026-01-01T08:00:00+00:00",
    )


def test_flair_separe_les_base_rates_par_verdict_basis() -> None:
    rows = [
        *(
            {
                "id": index,
                "symbol": f"ALLOC{index}",
                "family": "alpha",
                "verdict": "gagnant",
                "verdict_basis": "allocation",
            }
            for index in range(1, 4)
        ),
        *(
            {
                "id": index,
                "symbol": f"DIR{index}",
                "family": "beta",
                "verdict": "perdant",
                "verdict_basis": "direction",
            }
            for index in range(4, 7)
        ),
    ]
    result = score_selection_outcomes(rows, shrinkage_k=5.0)
    assert set(result["scores"]) == {1, 2, 3, 4, 5, 6}
    assert set(result["base_rates"]) == {"allocation", "direction"}
    # Groupes trop petits pour un BR famille → BR global du groupe.
    # Allocation 3 WIN → 1.0 ; direction 3 LOSS → 0.0. Mélangés → 0.5 partout.
    assert result["base_rates"]["allocation"]["ALLOC1"] == 1.0
    assert result["base_rates"]["direction"]["DIR4"] == 0.0
    assert all(result["scores"][index] == 0.0 for index in range(1, 7))


def test_flair_ids_restent_uniques_a_travers_les_groupes() -> None:
    rows = [
        {"id": 10, "symbol": "AAA", "family": "alpha", "verdict": "gagnant", "verdict_basis": "allocation"},
        {"id": 11, "symbol": "BBB", "family": "alpha", "verdict": "perdant", "verdict_basis": "direction"},
        {"id": 12, "symbol": "CCC", "family": "beta", "verdict": "neutre"},
    ]
    result = score_selection_outcomes(rows, shrinkage_k=5.0)
    assert set(result["scores"]) == {10, 11, 12}
    assert result["scored"] == 3


def test_flair_vue_souple_ne_contamine_pas_le_base_rate_dur() -> None:
    hard = [
        {
            "id": index,
            "symbol": f"H{index}",
            "family": "tw",
            "verdict": "perdant",
            "verdict_basis": "direction",
            "direction_source": "allowed_sides",
        }
        for index in range(1, 4)
    ]
    soft = [
        {
            "id": index,
            "symbol": f"S{index}",
            "family": "eu",
            "verdict": "gagnant",
            "verdict_basis": "direction",
            "direction_source": "directional_view",
        }
        for index in range(4, 7)
    ]
    hard_only = score_selection_outcomes(hard, shrinkage_k=5.0)
    mixed = score_selection_outcomes(hard + soft, shrinkage_k=5.0)
    assert hard_only["base_rates"]["direction"]["H1"] == 0.0
    assert mixed["base_rates"]["direction"]["H1"] == 0.0
    assert mixed["base_rates"][FLAIR_GROUP_DIRECTION_VIEW]["S4"] == 1.0
    assert hard_only["scores"][1] == mixed["scores"][1]
    assert "direction:directional_view" not in mixed["base_rates"]
    assert flair_scoring_group(hard[0]) == FLAIR_GROUP_DIRECTION
    assert flair_scoring_group(soft[0]) == FLAIR_GROUP_DIRECTION_VIEW
    assert flair_scoring_group({"verdict_basis": "allocation"}) == FLAIR_GROUP_ALLOCATION
    assert is_live_feedback_row(hard[0])
    assert not is_live_feedback_row(soft[0])
    assert is_live_feedback_row({"verdict_basis": "allocation"})
