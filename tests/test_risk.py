import math

import pytest

from trader.execution.risk import RiskGate, RiskLimits
from trader.execution.broker import Order


def _gate(*, max_risk_per_trade_pct: float = 0.01) -> RiskGate:
    return RiskGate(
        RiskLimits(
            max_position_value=20_000.0,
            max_gross_exposure=100_000.0,
            max_order_value=10_000.0,
            min_equity=50_000.0,
            max_risk_per_trade_pct=max_risk_per_trade_pct,
        )
    )


@pytest.mark.parametrize("bad_qty", [float("nan"), float("inf"), float("-inf")])
def test_check_rejette_quantity_non_finie(bad_qty) -> None:
    """Une quantity NaN/inf ne doit PAS contourner les bornes notionnelles
    (NaN > limite == False) : fast-fail explicite. Critique en mode exploration
    (gate confiance off + stop optionnel) où plus rien d'autre ne la borne."""
    gate = _gate()
    order = Order(symbol="SPY", side="BUY", quantity=bad_qty, rationale="x")
    verdict = gate.check(
        order,
        100.0,
        current_position_value=0.0,
        gross_exposure=0.0,
        equity=100_000.0,
    )
    assert verdict.approved is False


@pytest.mark.parametrize(
    ("equity", "entry_price", "stop_price", "expected_qty"),
    [
        (100_000.0, 100.0, 99.0, 1_000.0),
        (100_000.0, 100.0, 90.0, 100.0),
        (50_000.0, 100.0, 90.0, 50.0),
    ],
)
def test_max_quantity_at_risk_borne_la_taille_par_distance_au_stop(
    equity: float,
    entry_price: float,
    stop_price: float,
    expected_qty: float,
) -> None:
    gate = _gate()

    qty = gate.max_quantity_at_risk(equity, entry_price, stop_price)

    distance = abs(entry_price - stop_price)
    assert qty == pytest.approx(expected_qty)
    assert qty * distance <= gate.limits.max_risk_per_trade_pct * equity


@pytest.mark.parametrize(
    ("equity", "entry_price", "stop_price"),
    [
        (100_000.0, 100.0, 100.0),
        (0.0, 100.0, 90.0),
        (-1.0, 100.0, 90.0),
        (100_000.0, math.inf, 90.0),
        (100_000.0, 100.0, math.nan),
    ],
)
def test_max_quantity_at_risk_renvoie_zero_aux_bornes_non_exploitables(
    equity: float,
    entry_price: float,
    stop_price: float,
) -> None:
    assert _gate().max_quantity_at_risk(equity, entry_price, stop_price) == 0.0


def test_max_quantity_at_risk_arrondit_vers_le_bas_pour_respecter_linvariant() -> None:
    gate = _gate()
    entry_price = 100.0
    stop_price = 99.97
    distance = abs(entry_price - stop_price)

    qty = gate.max_quantity_at_risk(100_000.0, entry_price, stop_price)

    assert qty * distance <= gate.limits.max_risk_per_trade_pct * 100_000.0
    assert math.nextafter(qty, math.inf) * distance > gate.limits.max_risk_per_trade_pct * 100_000.0


def test_risk_limits_from_dict_defaut_a_un_pourcent() -> None:
    limits = RiskLimits.from_dict(
        {
            "max_position_value": 20_000.0,
            "max_gross_exposure": 100_000.0,
            "max_order_value": 10_000.0,
            "min_equity": 50_000.0,
        }
    )

    assert limits.max_risk_per_trade_pct == 0.01


def test_risk_gate_n_applique_plus_de_cap_count_par_cycle() -> None:
    limits = RiskLimits.from_dict(
        {
            "max_position_value": 20_000.0,
            "max_gross_exposure": 100_000.0,
            "max_order_value": 10_000.0,
            "min_equity": 50_000.0,
        }
    )
    gate = RiskGate(limits)
    order = Order(symbol="SPY", side="BUY", quantity=1.0, rationale="count removed")

    for _ in range(10):
        verdict = gate.check(
            order,
            100.0,
            current_position_value=0.0,
            gross_exposure=0.0,
            equity=100_000.0,
        )
        assert verdict.approved is True


# ---------------------------------------------------------------------------
# Confidence gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("planned_risk_pct", "expected_required"),
    [
        (0.0,    0.7),   # risque nul → seuil minimal
        (0.005,  0.8),   # demi-budget → point médian
        (0.01,   0.9),   # plein budget → seuil maximal
        (0.02,   0.9),   # au-delà du budget → clampé à 0.9
    ],
)
def test_required_confidence_lineaire_avec_le_risque_planifie(
    planned_risk_pct: float,
    expected_required: float,
) -> None:
    gate = _gate()  # max_risk_per_trade_pct=0.01

    assert gate.required_confidence(planned_risk_pct) == pytest.approx(expected_required)


@pytest.mark.parametrize(
    "bad_risk",
    [None, math.nan, math.inf, -math.inf],
)
def test_required_confidence_risque_non_borne_exige_full_confidence(bad_risk) -> None:
    # Sans hard stop, le risque n'est pas borné → on exige la confiance maximale.
    gate = _gate()

    assert gate.required_confidence(bad_risk) == pytest.approx(0.9)


def test_check_confidence_cas_reel_regression_short_clf_09_juin() -> None:
    """Régression : short CL=F du 09/06 confidence=0.58, risk_pct=0.00068 → −101 $."""
    gate = _gate()

    verdict = gate.check_confidence(confidence=0.58, planned_risk_pct=0.00068)

    assert verdict.approved is False
    assert verdict.code == "confidence_below_required"
    assert "0.58" in verdict.context
    assert "0.00068" in verdict.context


def test_check_confidence_approuve_quand_suffisante() -> None:
    gate = _gate()

    verdict = gate.check_confidence(confidence=0.75, planned_risk_pct=0.00068)

    assert verdict.approved is True


def test_check_confidence_none_rejet_fail_safe() -> None:
    gate = _gate()

    verdict = gate.check_confidence(confidence=None, planned_risk_pct=0.005)

    assert verdict.approved is False
    assert verdict.code == "confidence_below_required"


def test_risk_limits_from_dict_defauts_confidence() -> None:
    """Sans les clés, défauts 0.7/0.9 appliqués (rétrocompatibilité)."""
    limits = RiskLimits.from_dict(
        {
            "max_position_value": 20_000.0,
            "max_gross_exposure": 100_000.0,
            "max_order_value": 10_000.0,
            "min_equity": 50_000.0,
        }
    )

    assert limits.min_trade_confidence == 0.7
    assert limits.full_risk_confidence == 0.9
    assert limits.confidence_gate_enabled is True  # défaut live-safe


def test_check_confidence_desactive_approuve_meme_tres_basse() -> None:
    """confidence_gate_enabled=False → le gate confiance ne rejette plus rien,
    même une confiance ridicule au budget de risque plein (mode exploration paper)."""
    gate = RiskGate(
        RiskLimits(
            max_position_value=30_000.0,
            max_gross_exposure=100_000.0,
            max_order_value=10_000.0,
            min_equity=50_000.0,
            max_risk_per_trade_pct=0.01,
            confidence_gate_enabled=False,
        )
    )
    assert gate.check_confidence(0.05, 0.01).approved is True
    # None / hors-domaine ne doivent plus rejeter non plus quand le gate est off
    assert gate.check_confidence(None, None).approved is True


def test_risk_limits_from_dict_confidence_gate_desactivable() -> None:
    """La clé confidence_gate_enabled est lue si présente."""
    limits = RiskLimits.from_dict(
        {
            "max_position_value": 20_000.0,
            "max_gross_exposure": 100_000.0,
            "max_order_value": 10_000.0,
            "min_equity": 50_000.0,
            "confidence_gate_enabled": False,
        }
    )

    assert limits.confidence_gate_enabled is False


def test_risk_limits_from_dict_overrides_confidence() -> None:
    """Les clés sont lues si présentes."""
    limits = RiskLimits.from_dict(
        {
            "max_position_value": 20_000.0,
            "max_gross_exposure": 100_000.0,
            "max_order_value": 10_000.0,
            "min_equity": 50_000.0,
            "min_trade_confidence": 0.65,
            "full_risk_confidence": 0.85,
        }
    )

    assert limits.min_trade_confidence == 0.65
    assert limits.full_risk_confidence == 0.85


# ---------------------------------------------------------------------------
# Finding MAJOR 1 — validation des seuils de confiance (fail-closed)
# ---------------------------------------------------------------------------


def _base_limits_dict() -> dict:
    return {
        "max_position_value": 20_000.0,
        "max_gross_exposure": 100_000.0,
        "max_order_value": 10_000.0,
        "min_equity": 50_000.0,
    }


@pytest.mark.parametrize(
    ("min_tc", "full_rc"),
    [
        (math.nan, 0.9),         # min_trade_confidence nan → ValueError
        (0.7, math.nan),         # full_risk_confidence nan → ValueError
        (math.inf, 0.9),         # non-fini
        (0.7, math.inf),         # non-fini
    ],
)
def test_risk_limits_rejette_seuils_confiance_non_finis(min_tc, full_rc) -> None:
    """Un seuil non-fini rend le gate inutilisable → ValueError au chargement."""
    d = {**_base_limits_dict(), "min_trade_confidence": min_tc, "full_risk_confidence": full_rc}
    with pytest.raises(ValueError):
        RiskLimits.from_dict(d)


def test_risk_limits_rejette_min_superieur_a_full() -> None:
    """min_trade_confidence > full_risk_confidence est incohérent → ValueError."""
    d = {**_base_limits_dict(), "min_trade_confidence": 0.9, "full_risk_confidence": 0.7}
    with pytest.raises(ValueError):
        RiskLimits.from_dict(d)


@pytest.mark.parametrize(
    ("min_tc", "full_rc"),
    [
        (-0.1, 0.9),   # min hors [0,1]
        (0.7, 1.5),    # full hors [0,1]
        (-0.1, 1.5),   # les deux hors domaine
    ],
)
def test_risk_limits_rejette_seuils_confiance_hors_domaine(min_tc, full_rc) -> None:
    """Seuils hors [0,1] → ValueError."""
    d = {**_base_limits_dict(), "min_trade_confidence": min_tc, "full_risk_confidence": full_rc}
    with pytest.raises(ValueError):
        RiskLimits.from_dict(d)


@pytest.mark.parametrize(
    ("min_tc", "full_rc"),
    [
        (0.0, 0.0),    # bornes minimales acceptées
        (1.0, 1.0),    # bornes maximales acceptées (min == full autorisé)
        (0.0, 1.0),    # plage complète
    ],
)
def test_risk_limits_accepte_seuils_confiance_aux_bornes_valides(min_tc, full_rc) -> None:
    """Valeurs aux bornes [0,1] avec min <= full → pas d'erreur."""
    d = {**_base_limits_dict(), "min_trade_confidence": min_tc, "full_risk_confidence": full_rc}
    limits = RiskLimits.from_dict(d)
    assert limits.min_trade_confidence == min_tc
    assert limits.full_risk_confidence == full_rc


# ---------------------------------------------------------------------------
# Finding MAJOR 2 — confidence hors domaine [0,1]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_conf",
    [1.2, 999.0, 1.0001],
)
def test_check_confidence_rejette_confidence_superieure_a_un(bad_conf) -> None:
    """Confidence > 1 est hors domaine → rejet même si > required."""
    gate = _gate()

    verdict = gate.check_confidence(confidence=bad_conf, planned_risk_pct=0.0)

    assert verdict.approved is False
    assert verdict.code == "confidence_below_required"
    assert "out_of_domain" in verdict.context


@pytest.mark.parametrize(
    "bad_conf",
    [-0.1, -1.0, -0.0001],
)
def test_check_confidence_rejette_confidence_negative(bad_conf) -> None:
    """Confidence < 0 est hors domaine → rejet."""
    gate = _gate()

    verdict = gate.check_confidence(confidence=bad_conf, planned_risk_pct=0.0)

    assert verdict.approved is False
    assert verdict.code == "confidence_below_required"
    assert "out_of_domain" in verdict.context


def test_check_confidence_accepte_confidence_un_avec_risque_faible() -> None:
    """Confidence exactement 1.0 est valide et doit être approuvée."""
    gate = _gate()

    verdict = gate.check_confidence(confidence=1.0, planned_risk_pct=0.0)

    assert verdict.approved is True


def test_check_confidence_nan_affiche_nan_dans_contexte() -> None:
    """NaN en entrée → le context doit afficher 'nan', pas 'None'."""
    gate = _gate()

    verdict = gate.check_confidence(confidence=math.nan, planned_risk_pct=0.005)

    assert verdict.approved is False
    assert "nan" in verdict.context
    assert "None" not in verdict.context


# ---------------------------------------------------------------------------
# Task 4 — sizing et bornes de risque en USD via fx_rate
# ---------------------------------------------------------------------------


def _gate_usd(max_order_value: float = 10_000.0, pct: float = 0.01) -> RiskGate:
    """Gate minimal pour les tests fx_rate (tous les autres champs à des valeurs neutres)."""
    return RiskGate(
        RiskLimits(
            max_position_value=200_000.0,
            max_gross_exposure=1_000_000.0,
            max_order_value=max_order_value,
            min_equity=0.0,
            max_risk_per_trade_pct=pct,
        )
    )


def test_order_qty_respects_usd_cap_for_twd() -> None:
    """qty * price_native * fx_rate <= max_order_value (plafond USD respecté)."""
    gate = _gate_usd(max_order_value=10_000.0)
    qty = gate.max_order_quantity_at_price(870.0, fx_rate=0.031)
    # valeur USD de l'ordre <= 10000
    assert qty * 870.0 * 0.031 <= 10_000.0 + 1e-6
    # et nettement plus que l'ancien 10000/870 ≈ 11
    assert qty > 300.0


def test_qty_at_risk_uses_usd_distance() -> None:
    """qty * distance_native * fx_rate == risk_cap exactement (arrondi vers le bas)."""
    gate = _gate_usd(pct=0.01)
    # equity 100k USD, distance native 40 TWD, rate 0.031 -> risque USD ciblé 1000
    qty = gate.max_quantity_at_risk(100_000.0, 870.0, 830.0, fx_rate=0.031)
    real_risk_usd = qty * abs(870.0 - 830.0) * 0.031
    assert real_risk_usd == pytest.approx(1_000.0, rel=1e-6)


def test_order_qty_fx_rate_default_unchanged() -> None:
    """fx_rate=1.0 par défaut → comportement USD identique à l'ancien."""
    gate = _gate_usd(max_order_value=10_000.0)
    qty_default = gate.max_order_quantity_at_price(100.0)
    qty_explicit = gate.max_order_quantity_at_price(100.0, fx_rate=1.0)
    assert qty_default == qty_explicit
    assert qty_default * 100.0 <= 10_000.0 + 1e-6


def test_qty_at_risk_fx_rate_default_unchanged() -> None:
    """fx_rate=1.0 par défaut → comportement USD identique à l'ancien."""
    gate = _gate_usd(pct=0.01)
    qty_default = gate.max_quantity_at_risk(100_000.0, 100.0, 99.0)
    qty_explicit = gate.max_quantity_at_risk(100_000.0, 100.0, 99.0, fx_rate=1.0)
    assert qty_default == qty_explicit


def test_order_qty_fx_rate_zero_returns_zero() -> None:
    """fx_rate <= 0 est invalide → retourne 0."""
    gate = _gate_usd()
    assert gate.max_order_quantity_at_price(100.0, fx_rate=0.0) == 0.0
    assert gate.max_order_quantity_at_price(100.0, fx_rate=-1.0) == 0.0


def test_qty_at_risk_fx_rate_invalid_returns_zero() -> None:
    """fx_rate non-fini ou <= 0 → retourne 0."""
    gate = _gate_usd()
    assert gate.max_quantity_at_risk(100_000.0, 100.0, 99.0, fx_rate=0.0) == 0.0
    assert gate.max_quantity_at_risk(100_000.0, 100.0, 99.0, fx_rate=math.nan) == 0.0


# ---------------------------------------------------------------------------
# Task 5 — gate.check fx_rate : ordre natif TWD converti en USD
# ---------------------------------------------------------------------------


def test_check_fx_rate_converts_order_value_to_usd() -> None:
    """gate.check avec fx_rate convertit la valeur de l'ordre en USD.

    TWD : qty=101, price=870, fx_rate=0.031 → order_value_usd ≈ 2727 < 10000 → APPROVED.
    Sans conversion (fx_rate=1.0) : order_value_native = 101*870 = 87870 > 10000 → REJECTED.
    """
    from trader.execution.broker import Order  # noqa: PLC0415
    gate = _gate_usd(max_order_value=10_000.0)

    order = Order(symbol="2379.TW", side="BUY", quantity=101.0)

    # Avec fx_rate TWD : order_value_usd = 101 * 870 * 0.031 ≈ 2727 → approuvé
    verdict_fx = gate.check(
        order,
        870.0,
        current_position_value=0.0,
        gross_exposure=0.0,
        equity=200_000.0,
        fx_rate=0.031,
    )
    assert verdict_fx.approved is True, f"expected APPROVED with fx_rate, got {verdict_fx.code}"

    # Sans conversion (fx_rate=1.0) : order_value = 101 * 870 = 87870 > 10000 → rejeté
    verdict_no_fx = gate.check(
        order,
        870.0,
        current_position_value=0.0,
        gross_exposure=0.0,
        equity=200_000.0,
        fx_rate=1.0,
    )
    assert verdict_no_fx.approved is False, "expected REJECTED without fx_rate"
    assert verdict_no_fx.code == "order_value_exceeded"
