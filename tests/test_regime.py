"""Tests TDD pour trader.regime — classifieur déterministe de régime de marché.

Priorité aux cas limites (AX: Test-First Invariants).
"""

from __future__ import annotations

import pytest

from trader.regime import (
    CANDLE_SIGNAL_LABELS,
    ER_TREND_THRESHOLD,
    VOL_HIGH_THRESHOLD,
    VOL_LOW_THRESHOLD,
    Z_STRETCHED_THRESHOLD,
    MarketRegime,
    classify_regime,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ind(**kwargs) -> dict:
    """Construit un dict d'indicateurs minimal. Valeurs absentes = champs manquants."""
    return kwargs


# ---------------------------------------------------------------------------
# 1. Cas dégradé : entrées manquantes ou None
# ---------------------------------------------------------------------------


def test_tous_indicateurs_none_renvoie_regime_unknown() -> None:
    ind = _ind(
        efficiency_ratio=None,
        trend_slope=None,
        chart_breakout=None,
        volatility=None,
        z_score=None,
        candlestick_signal=None,
    )
    r = classify_regime(ind)
    assert r.regime == "unknown"
    assert r.vol_state is None
    assert r.stretched is None
    assert r.candle is None


def test_dict_vide_renvoie_regime_unknown() -> None:
    r = classify_regime({})
    assert r.regime == "unknown"
    assert r.vol_state is None
    assert r.stretched is None
    assert r.candle is None


def test_indicateur_manquant_ne_plante_pas() -> None:
    """Aucun des indicateurs présents → pas d'exception, dégradation gracieuse."""
    r = classify_regime({"return": 0.01})
    assert r.regime == "unknown"


def test_efficiency_ratio_none_renvoie_unknown() -> None:
    ind = _ind(efficiency_ratio=None, trend_slope=0.005, chart_breakout=0.0, volatility=0.01, z_score=0.5, candlestick_signal=0.0)
    r = classify_regime(ind)
    assert r.regime == "unknown"


def test_z_score_none_stretched_est_none() -> None:
    """z_score absent → stretched=None (on ne peut pas affirmer 'pas étiré')."""
    ind = _ind(efficiency_ratio=0.4, trend_slope=0.003, chart_breakout=0.0, volatility=0.01, z_score=None, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.stretched is None


def test_volatility_none_vol_state_est_none() -> None:
    ind = _ind(efficiency_ratio=0.4, trend_slope=0.003, chart_breakout=0.0, volatility=None, z_score=1.0, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.vol_state is None


# ---------------------------------------------------------------------------
# 2. Régime trending (ER >= seuil)
# ---------------------------------------------------------------------------


def test_er_haut_slope_positif_renvoie_trending_up() -> None:
    ind = _ind(
        efficiency_ratio=ER_TREND_THRESHOLD + 0.1,
        trend_slope=0.005,
        chart_breakout=0.0,
        volatility=0.01,
        z_score=0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "trending_up"


def test_er_haut_slope_negatif_renvoie_trending_down() -> None:
    ind = _ind(
        efficiency_ratio=ER_TREND_THRESHOLD + 0.1,
        trend_slope=-0.005,
        chart_breakout=0.0,
        volatility=0.01,
        z_score=0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "trending_down"


def test_er_haut_slope_none_renvoie_unknown() -> None:
    """ER trending mais direction inconnue → unknown (ne fabrique pas de direction)."""
    ind = _ind(
        efficiency_ratio=ER_TREND_THRESHOLD + 0.1,
        trend_slope=None,
        chart_breakout=0.0,
        volatility=0.01,
        z_score=0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "unknown"


# ---------------------------------------------------------------------------
# 3. Seuil exact de l'ER (frontière >= vs >)
# ---------------------------------------------------------------------------


def test_er_exactement_au_seuil_est_trending() -> None:
    """ER == ER_TREND_THRESHOLD → trending (boundary incluse côté trend)."""
    ind = _ind(
        efficiency_ratio=ER_TREND_THRESHOLD,
        trend_slope=0.003,
        chart_breakout=0.0,
        volatility=0.01,
        z_score=0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "trending_up"


def test_er_en_dessous_du_seuil_est_range() -> None:
    ind = _ind(
        efficiency_ratio=ER_TREND_THRESHOLD - 0.001,
        trend_slope=0.003,
        chart_breakout=0.0,
        volatility=0.01,
        z_score=0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "range"


# ---------------------------------------------------------------------------
# 4. Breakout prime sur ER (même ER bas)
# ---------------------------------------------------------------------------


def test_breakout_up_prime_meme_si_er_bas() -> None:
    ind = _ind(
        efficiency_ratio=0.05,          # ER très bas → chop
        trend_slope=0.001,
        chart_breakout=1.0,             # cassure haussière active
        volatility=0.01,
        z_score=0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "breakout"


def test_breakout_down_prime_meme_si_er_bas() -> None:
    ind = _ind(
        efficiency_ratio=0.05,
        trend_slope=-0.001,
        chart_breakout=-1.0,            # cassure baissière active
        volatility=0.01,
        z_score=-0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "breakout"


def test_chart_breakout_zero_ne_declenche_pas_breakout() -> None:
    """chart_breakout=0.0 (inside range) → pas de breakout."""
    ind = _ind(
        efficiency_ratio=0.05,
        trend_slope=0.001,
        chart_breakout=0.0,
        volatility=0.01,
        z_score=0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "range"


def test_chart_breakout_none_traite_comme_non_actif() -> None:
    """chart_breakout=None → pas de breakout, pas d'unknown non plus."""
    ind = _ind(
        efficiency_ratio=0.05,
        trend_slope=0.001,
        chart_breakout=None,
        volatility=0.01,
        z_score=0.5,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "range"


# ---------------------------------------------------------------------------
# 5. vol_state
# ---------------------------------------------------------------------------


def test_vol_basse_renvoie_low() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=VOL_LOW_THRESHOLD - 0.001, z_score=0.0, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.vol_state == "low"


def test_vol_haute_renvoie_high() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=VOL_HIGH_THRESHOLD + 0.001, z_score=0.0, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.vol_state == "high"


def test_vol_dans_la_plage_normale_renvoie_normal() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=(VOL_LOW_THRESHOLD + VOL_HIGH_THRESHOLD) / 2,
               z_score=0.0, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.vol_state == "normal"


def test_vol_exactement_au_seuil_high_est_high() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=VOL_HIGH_THRESHOLD, z_score=0.0, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.vol_state == "high"


def test_vol_exactement_au_seuil_low_est_low() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=VOL_LOW_THRESHOLD, z_score=0.0, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.vol_state == "low"


# ---------------------------------------------------------------------------
# 6. stretched
# ---------------------------------------------------------------------------


def test_z_score_extreme_positif_est_stretched() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=Z_STRETCHED_THRESHOLD + 0.1, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.stretched is True


def test_z_score_extreme_negatif_est_stretched() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=-(Z_STRETCHED_THRESHOLD + 0.1), candlestick_signal=None)
    r = classify_regime(ind)
    assert r.stretched is True


def test_z_score_modere_nest_pas_stretched() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=1.0, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.stretched is False


def test_z_score_exactement_au_seuil_est_stretched() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=Z_STRETCHED_THRESHOLD, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.stretched is True


# ---------------------------------------------------------------------------
# 7. candlestick_signal → champ candle
# ---------------------------------------------------------------------------


def test_candle_signal_positif_fort_renvoie_bullish_engulfing() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=0.0, candlestick_signal=1.0)
    r = classify_regime(ind)
    assert r.candle == "bullish_engulfing"


def test_candle_signal_negatif_fort_renvoie_bearish_engulfing() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=0.0, candlestick_signal=-1.0)
    r = classify_regime(ind)
    assert r.candle == "bearish_engulfing"


def test_candle_signal_demi_positif_renvoie_hammer() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=0.0, candlestick_signal=0.5)
    r = classify_regime(ind)
    assert r.candle == "hammer"


def test_candle_signal_demi_negatif_renvoie_shooting_star() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=0.0, candlestick_signal=-0.5)
    r = classify_regime(ind)
    assert r.candle == "shooting_star"


def test_candle_signal_zero_renvoie_none() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=0.0, candlestick_signal=0.0)
    r = classify_regime(ind)
    assert r.candle is None


def test_candle_signal_none_renvoie_none() -> None:
    ind = _ind(efficiency_ratio=0.1, trend_slope=0.0, chart_breakout=0.0,
               volatility=0.01, z_score=0.0, candlestick_signal=None)
    r = classify_regime(ind)
    assert r.candle is None


# ---------------------------------------------------------------------------
# 8. Combinaisons sémantiques
# ---------------------------------------------------------------------------


def test_range_stretched_bullish_engulfing_confluence_reversion() -> None:
    """range + stretched + bougie de retournement haussière = combo réversion fort."""
    ind = _ind(
        efficiency_ratio=0.1,
        trend_slope=0.0,
        chart_breakout=0.0,
        volatility=0.01,
        z_score=-(Z_STRETCHED_THRESHOLD + 0.5),
        candlestick_signal=1.0,
    )
    r = classify_regime(ind)
    assert r.regime == "range"
    assert r.stretched is True
    assert r.candle == "bullish_engulfing"


def test_breakout_shooting_star_confluence_confirmation() -> None:
    """breakout down + shooting star = combo continuation baissière."""
    ind = _ind(
        efficiency_ratio=0.1,
        trend_slope=-0.002,
        chart_breakout=-1.0,
        volatility=0.025,
        z_score=-2.5,
        candlestick_signal=-0.5,
    )
    r = classify_regime(ind)
    assert r.regime == "breakout"
    assert r.vol_state == "high"
    assert r.stretched is True
    assert r.candle == "shooting_star"


def test_trending_up_high_vol_stretched() -> None:
    """trending_up + vol haute + étiré → régime de fin de trend potentiel."""
    ind = _ind(
        efficiency_ratio=ER_TREND_THRESHOLD + 0.2,
        trend_slope=0.01,
        chart_breakout=0.0,
        volatility=VOL_HIGH_THRESHOLD + 0.005,
        z_score=Z_STRETCHED_THRESHOLD + 0.3,
        candlestick_signal=0.0,
    )
    r = classify_regime(ind)
    assert r.regime == "trending_up"
    assert r.vol_state == "high"
    assert r.stretched is True
    assert r.candle is None


# ---------------------------------------------------------------------------
# 9. MarketRegime est un dataclass frozen (immuabilité)
# ---------------------------------------------------------------------------


def test_market_regime_est_frozen() -> None:
    r = classify_regime({})
    with pytest.raises((AttributeError, TypeError)):
        r.regime = "trending_up"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 10. CANDLE_SIGNAL_LABELS est un mapping exhaustif des valeurs réelles
# ---------------------------------------------------------------------------


def test_candle_signal_labels_couvre_les_valeurs_reelles() -> None:
    """Les 4 valeurs non-nulles possibles de candlestick_signal ont chacune un label."""
    for val in [1.0, -1.0, 0.5, -0.5]:
        assert val in CANDLE_SIGNAL_LABELS
        assert CANDLE_SIGNAL_LABELS[val] is not None
