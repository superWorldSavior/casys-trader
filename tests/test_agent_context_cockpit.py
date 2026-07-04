"""Tests TDD pour build_market_cockpit — intégration du module régime.

Vérifie que les colonnes de régime sont présentes et cohérentes,
et que la structure existante (cols numériques, highlights) est préservée.
"""

from __future__ import annotations

import trader.agent.context as agent_context
import trader.market.regime as regime
from trader.agent.context import build_market_cockpit
from trader.market.market_data import Bar


def _bar(close: float, high: float | None = None, low: float | None = None) -> Bar:
    return Bar(
        ts="2026-06-08T10:00:00+00:00",
        open=close - 0.5,
        high=high if high is not None else close + 1.0,
        low=low if low is not None else close - 1.0,
        close=close,
        volume=1000.0,
    )


def _trending_bars(base: float = 100.0, n: int = 12) -> list[Bar]:
    """Série avec forte tendance (ER élevé attendu)."""
    return [_bar(base + i * 0.5) for i in range(n)]


def _choppy_bars(base: float = 100.0, n: int = 12) -> list[Bar]:
    """Série en range (prix oscillent sans trend net)."""
    import math
    return [_bar(base + math.sin(i) * 0.5) for i in range(n)]


def _timed_trending_15m(base: float = 100.0, n: int = 32) -> list[Bar]:
    """Série 15m trendée, sans breakout artificiel grâce aux mèches larges."""
    return [
        Bar(
            ts=f"2026-06-05T{8 + index // 4:02d}:{(index % 4) * 15:02d}:00+00:00",
            open=base + index * 0.5 - 0.25,
            high=1000.0,
            low=0.0,
            close=base + index * 0.5,
            volume=1000.0,
        )
        for index in range(n)
    ]


def _daily_breakout_up() -> list[Bar]:
    return [
        Bar(ts="2026-06-01T00:00:00+00:00", open=99.5, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts="2026-06-02T00:00:00+00:00", open=100.0, high=101.5, low=99.5, close=100.5, volume=1000.0),
        Bar(ts="2026-06-03T00:00:00+00:00", open=104.5, high=105.2, low=104.0, close=105.0, volume=1000.0),
    ]


def _daily_trending_neutral(base: float = 200.0, n: int = 12) -> list[Bar]:
    return [
        Bar(
            ts=f"2026-06-{index + 1:02d}T00:00:00+00:00",
            open=base + index * 0.5,
            high=base + index * 0.5 + 1.0,
            low=base + index * 0.5 - 1.0,
            close=base + index * 0.5,
            volume=1000.0,
        )
        for index in range(n)
    ]


# ---------------------------------------------------------------------------
# Structure des colonnes
# ---------------------------------------------------------------------------


def test_cockpit_contient_les_colonnes_regime() -> None:
    bars = {"SPY": _trending_bars()}
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    cols = result["cols"]
    assert "reg" in cols
    assert "vs" in cols
    assert "st" in cols
    assert "cndle" in cols
    assert "htf" in cols
    assert "aligned" in cols
    assert "sig" in cols


def test_cockpit_preserve_les_colonnes_numeriques_existantes() -> None:
    bars = {"SPY": _trending_bars()}
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    cols = result["cols"]
    for col in ["s", "f", "p", "r", "vol", "z", "er", "ac"]:
        assert col in cols, f"colonne {col!r} manquante"


def test_cockpit_ajoute_les_colonnes_daily_avant_le_regime() -> None:
    bars = {"SPY": _trending_bars()}
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
        daily_bars_by_symbol={"SPY": _daily_trending_neutral()},
    )

    cols = result["cols"]

    assert cols[3:10] == ["r", "vol", "z", "er", "ac", "rs", "sz"]
    assert cols[10:17] == ["r_d", "vol_d", "z_d", "er_d", "ac_d", "rs_d", "sz_d"]
    assert cols[17:21] == ["reg", "vs", "st", "cndle"]
    sig_idx = cols.index("sig")
    assert cols[sig_idx - 2 : sig_idx + 1] == ["htf", "aligned", "sig"]


def test_cockpit_calcule_rs_daily_distinct_du_rs_court() -> None:
    bars = {
        "SPY": [_bar(close) for close in [100.0, 101.0, 102.0, 110.0]],
        "QQQ": [_bar(close) for close in [200.0, 200.0, 200.0, 200.0]],
        "DIA": [_bar(close) for close in [300.0, 300.0, 300.0, 300.0]],
    }
    daily = {
        "SPY": [_bar(close) for close in [100.0, 99.0, 98.0, 97.0]],
        "QQQ": [_bar(close) for close in [200.0, 202.0, 204.0, 206.0]],
        "DIA": [_bar(close) for close in [300.0, 303.0, 306.0, 309.0]],
    }

    result = build_market_cockpit(
        bars,
        symbols=["SPY", "QQQ", "DIA"],
        prices={"SPY": 110.0, "QQQ": 200.0, "DIA": 300.0},
        window=4,
        daily_bars_by_symbol=daily,
    )

    cols = result["cols"]
    spy_row = next(row for row in result["rows"] if row[0] == "SPY")

    assert spy_row[cols.index("rs")] > 0
    assert spy_row[cols.index("rs_d")] < 0
    assert spy_row[cols.index("rs")] != spy_row[cols.index("rs_d")]


def test_cockpit_colonnes_regime_apres_colonnes_numeriques() -> None:
    """Les colonnes régime doivent être APRÈS les numériques (offset rank_by_abs safe)."""
    bars = {"SPY": _trending_bars()}
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    cols = result["cols"]
    er_index = cols.index("er")
    reg_index = cols.index("reg")
    assert reg_index > er_index


# ---------------------------------------------------------------------------
# Contenu des lignes
# ---------------------------------------------------------------------------


def test_cockpit_row_contient_le_regime_spy() -> None:
    bars = {"SPY": _trending_bars()}
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    cols = result["cols"]
    spy_row = next(row for row in result["rows"] if row[0] == "SPY")
    reg_idx = cols.index("reg")
    vs_idx = cols.index("vs")
    st_idx = cols.index("st")
    cndle_idx = cols.index("cndle")

    assert spy_row[reg_idx] in {"trending_up", "trending_down", "range", "breakout", "unknown"}
    assert spy_row[vs_idx] in {"low", "normal", "high", None}
    assert spy_row[st_idx] in {True, False, None}
    # candle peut être None ou une string
    assert spy_row[cndle_idx] is None or isinstance(spy_row[cndle_idx], str)


def test_cockpit_serie_vide_regime_unknown() -> None:
    """Symbole sans barres → regime=unknown, vol_state=None, stretched=None."""
    result = build_market_cockpit(
        {},
        symbols=["SPY"],
        prices={"SPY": 100.0},
        window=12,
    )
    cols = result["cols"]
    spy_row = next(row for row in result["rows"] if row[0] == "SPY")
    reg_idx = cols.index("reg")
    vs_idx = cols.index("vs")
    st_idx = cols.index("st")

    assert spy_row[reg_idx] == "unknown"
    assert spy_row[vs_idx] is None
    assert spy_row[st_idx] is None


def test_cockpit_highlights_toujours_presents() -> None:
    """rank_by_abs ne doit pas casser avec les nouvelles colonnes."""
    bars = {"SPY": _trending_bars(), "QQQ": _choppy_bars(base=200.0)}
    result = build_market_cockpit(
        bars,
        symbols=["SPY", "QQQ"],
        prices={"SPY": 105.0, "QQQ": 202.0},
        window=12,
    )
    assert "highlights" in result
    assert "abs_r" in result["highlights"]
    assert "abs_z" in result["highlights"]
    assert "abs_sz" in result["highlights"]


def test_cockpit_version_est_cp3() -> None:
    """La version du cockpit est mise à jour à cp3 suite aux signaux multi-horizon."""
    bars = {"SPY": _trending_bars()}
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    assert result["v"] == "cp3"


def test_cockpit_schema_mentionne_regime() -> None:
    bars = {"SPY": _trending_bars()}
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    assert "regime" in result["schema"]
    assert "vol_state" in result["schema"]
    assert "stretched" in result["schema"]
    assert "candle_pattern" in result["schema"]
    assert "htf" in result["schema"]
    assert "aligned" in result["schema"]
    assert "sig" in result["schema"]


def test_cockpit_cp3_ajoute_signaux_multi_horizon_en_fin_de_ligne() -> None:
    bars = {"SPY": _timed_trending_15m(), "QQQ": _timed_trending_15m(base=200.0)}
    result = build_market_cockpit(
        bars,
        symbols=["SPY", "QQQ"],
        prices={"SPY": 116.0, "QQQ": 216.0},
        window=12,
        daily_bars_by_symbol={
            "SPY": _daily_breakout_up(),
            "QQQ": _daily_trending_neutral(base=200.0, n=12),
        },
    )
    cols = result["cols"]
    spy_row = next(row for row in result["rows"] if row[0] == "SPY")
    qqq_row = next(row for row in result["rows"] if row[0] == "QQQ")

    sig_idx = cols.index("sig")
    assert cols[sig_idx - 2 : sig_idx + 1] == ["htf", "aligned", "sig"]
    assert spy_row[cols.index("htf")] == "breakout"
    assert spy_row[cols.index("aligned")] is False
    assert spy_row[cols.index("sig")] == ["1d:breakout_up"]
    assert qqq_row[cols.index("htf")] == "trending_up"
    assert qqq_row[cols.index("aligned")] is True
    assert qqq_row[cols.index("sig")] is None


def test_cockpit_derive_1h_4h_depuis_15m_et_retombe_sur_4h_sans_daily() -> None:
    result = build_market_cockpit(
        {"SPY": _timed_trending_15m()},
        symbols=["SPY"],
        prices={"SPY": 116.0},
        window=12,
    )
    cols = result["cols"]
    spy_row = next(row for row in result["rows"] if row[0] == "SPY")

    assert spy_row[cols.index("htf")] == "trending_up"
    assert spy_row[cols.index("aligned")] is True


# ---------------------------------------------------------------------------
# Test d'intégration return_index (test_daemon_features compat)
# ---------------------------------------------------------------------------


def test_return_index_toujours_accessible_par_col_name() -> None:
    """Le test daemon accède au return via cockpit['cols'].index('r') + row[idx].
    Vérifier que cela marche encore après l'ajout des colonnes régime en fin.
    """
    bars = {
        "SPY": [
            Bar(ts="t1", open=99.5, high=101.0, low=99.0, close=100.0, volume=1000.0),
            Bar(ts="t2", open=100.5, high=102.0, low=100.0, close=102.0, volume=1000.0),
            Bar(ts="t3", open=101.5, high=103.0, low=101.0, close=104.0, volume=1000.0),
        ]
    }
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 104.0},
        window=3,
    )
    return_index = result["cols"].index("r")
    spy_row = next(row for row in result["rows"] if row[0] == "SPY")
    assert spy_row[return_index] == 0.04


# ---------------------------------------------------------------------------
# Colonnes swing (distance % aux swing_low/high, fenêtres 24 et 48)
# ---------------------------------------------------------------------------


def test_cockpit_ajoute_colonnes_swing_24_48() -> None:
    result = build_market_cockpit(
        {"SPY": _trending_bars()},
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    cols = result["cols"]
    for col in ["sl24", "sh24", "sl48", "sh48"]:
        assert col in cols, f"colonne swing {col!r} manquante"


def test_cockpit_swing_distance_signee_depuis_le_prix() -> None:
    # Une barre dont low=90, high=110 ; prix=100.
    #   sl = (100 - 90) / 100 = 0.1   (swing_low 10% SOUS le prix)
    #   sh = (110 - 100) / 100 = 0.1  (swing_high 10% AU-DESSUS du prix)
    result = build_market_cockpit(
        {"SPY": [_bar(100.0, high=110.0, low=90.0)]},
        symbols=["SPY"],
        prices={"SPY": 100.0},
        window=12,
    )
    cols = result["cols"]
    row = next(r for r in result["rows"] if r[0] == "SPY")
    assert row[cols.index("sl48")] == 0.1
    assert row[cols.index("sh48")] == 0.1
    assert row[cols.index("sl24")] == 0.1
    assert row[cols.index("sh24")] == 0.1


def test_cockpit_swing_none_sans_barres() -> None:
    result = build_market_cockpit(
        {},
        symbols=["SPY"],
        prices={"SPY": 100.0},
        window=12,
    )
    cols = result["cols"]
    row = next(r for r in result["rows"] if r[0] == "SPY")
    assert row[cols.index("sl24")] is None
    assert row[cols.index("sh48")] is None


def test_cockpit_swing_apres_sig_avant_fee() -> None:
    def estimator(symbol: str, price: float) -> dict:
        return {"be_bps": 2.0, "fee_rt": 2.0, "currency": "EUR"}

    result = build_market_cockpit(
        {"SPY": _trending_bars()},
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
        fee_estimator=estimator,
        fee_ref_notional=10_000.0,
    )
    cols = result["cols"]
    sig_idx = cols.index("sig")
    # swing juste après sig, frais avant les colonnes devise (en fin de ligne)
    assert cols[sig_idx + 1 : sig_idx + 5] == ["sl24", "sh24", "sl48", "sh48"]
    # frais précèdent les 4 colonnes devise (ccy, fx_usd, risk_budget_native, max_order_native)
    assert cols[-7:-4] == ["be_ref_bps", "fee", "fee_ccy"]
    assert cols[-4:] == ["ccy", "fx_usd", "risk_budget_native", "max_order_native"]


def test_cockpit_swing_dans_le_schema() -> None:
    result = build_market_cockpit(
        {"SPY": _trending_bars()},
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    assert "sl24" in result["schema"]
    assert "sh48" in result["schema"]


def test_cockpit_swing_n_affecte_pas_les_highlights() -> None:
    """rank_by_abs (offset COCKPIT_INDICATORS + 3) reste correct."""
    result = build_market_cockpit(
        {"SPY": _trending_bars(), "QQQ": _choppy_bars(base=200.0)},
        symbols=["SPY", "QQQ"],
        prices={"SPY": 105.0, "QQQ": 202.0},
        window=12,
    )
    return_index = result["cols"].index("r")
    spy_row = next(r for r in result["rows"] if r[0] == "SPY")
    assert isinstance(spy_row[return_index], float)
    assert "abs_z" in result["highlights"]


# ---------------------------------------------------------------------------
# Colonnes frais (conditionnelles : seulement si fee_estimator fourni)
# ---------------------------------------------------------------------------


def test_cockpit_sans_fee_estimator_n_ajoute_pas_de_colonnes_frais() -> None:
    """Comportement par défaut préservé : pas de fee_estimator → pas de colonnes."""
    result = build_market_cockpit(
        {"SPY": _trending_bars()},
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    assert "be_ref_bps" not in result["cols"]
    assert "fee" not in result["cols"]
    assert "fee_ccy" not in result["cols"]
    assert "fee_ref_notional" not in result


def test_cockpit_avec_fee_estimator_ajoute_be_ref_bps_fee_fee_ccy_en_fin_de_ligne() -> None:
    def estimator(symbol: str, price: float) -> dict:
        return {"be_bps": 2.0, "fee_rt": 2.0, "currency": "EUR"}

    result = build_market_cockpit(
        {"SPY": _trending_bars()},
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
        fee_estimator=estimator,
        fee_ref_notional=10_000.0,
    )
    cols = result["cols"]
    # frais précèdent les 4 colonnes devise (ccy, fx_usd, risk_budget_native, max_order_native)
    assert cols[-7:-4] == ["be_ref_bps", "fee", "fee_ccy"]
    assert cols[-4:] == ["ccy", "fx_usd", "risk_budget_native", "max_order_native"]
    assert result["fee_ref_notional"] == 10_000.0
    assert "be_ref_bps" in result["schema"]

    spy_row = next(row for row in result["rows"] if row[0] == "SPY")
    # be_ref_bps et fee sont NUMÉRIQUES (parsing agent), devise séparée
    assert spy_row[cols.index("be_ref_bps")] == 2.0
    assert spy_row[cols.index("fee")] == 2.0
    assert spy_row[cols.index("fee_ccy")] == "EUR"


def test_cockpit_fee_estimator_renvoyant_none_laisse_colonnes_vides() -> None:
    def estimator(symbol: str, price: float) -> None:
        return None

    result = build_market_cockpit(
        {"SPY": _trending_bars()},
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
        fee_estimator=estimator,
        fee_ref_notional=10_000.0,
    )
    cols = result["cols"]
    spy_row = next(row for row in result["rows"] if row[0] == "SPY")
    assert spy_row[cols.index("be_ref_bps")] is None
    assert spy_row[cols.index("fee")] is None
    assert spy_row[cols.index("fee_ccy")] is None


def test_cockpit_highlights_intacts_avec_colonnes_frais() -> None:
    """rank_by_abs reste correct malgré les colonnes frais appendées."""
    def estimator(symbol: str, price: float) -> dict:
        return {"be_bps": 3.0, "fee_rt": 3.0, "currency": "USD"}

    result = build_market_cockpit(
        {"SPY": _trending_bars(), "QQQ": _choppy_bars(base=200.0)},
        symbols=["SPY", "QQQ"],
        prices={"SPY": 105.0, "QQQ": 202.0},
        window=12,
        fee_estimator=estimator,
        fee_ref_notional=10_000.0,
    )
    assert "abs_r" in result["highlights"]
    return_index = result["cols"].index("r")
    spy_row = next(row for row in result["rows"] if row[0] == "SPY")
    assert isinstance(spy_row[return_index], float)


def test_cockpit_reutilise_les_indicateurs_15m_du_snapshot_pour_les_signaux(monkeypatch) -> None:
    base_bars = _timed_trending_15m(n=16)
    snapshot_indicators = {
        "return": 0.075,
        "volatility": 0.01,
        "z_score": 0.0,
        "efficiency_ratio": 1.0,
        "autocorrelation": None,
        "relative_strength": None,
        "spread_zscore": None,
        "trend_slope": 0.004,
        "chart_breakout": 0.0,
        "candlestick_signal": 0.0,
    }
    indicator_calls: list[list[Bar]] = []

    def fake_snapshot(bars_by_symbol, *, symbols, names, window):
        assert bars_by_symbol["SPY"] is base_bars
        return {
            "SPY": {
                "family": None,
                "window": window,
                "indicators": {name: snapshot_indicators.get(name) for name in names},
            }
        }

    def fake_compute_indicator_values(bars, *, names, window, **kwargs):
        indicator_calls.append(bars)
        return {
            "efficiency_ratio": 1.0,
            "trend_slope": 0.004,
            "chart_breakout": 0.0,
            "volatility": 0.01,
            "z_score": 0.0,
            "candlestick_signal": 0.0,
        }

    monkeypatch.setattr(agent_context, "build_indicator_snapshot", fake_snapshot)
    monkeypatch.setattr(regime, "compute_indicator_values", fake_compute_indicator_values)

    agent_context.build_market_cockpit(
        {"SPY": base_bars},
        symbols=["SPY"],
        prices={"SPY": 107.5},
        window=12,
    )

    assert all(call is not base_bars for call in indicator_calls)
