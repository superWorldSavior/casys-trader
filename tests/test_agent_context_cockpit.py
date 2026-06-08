"""Tests TDD pour build_market_cockpit — intégration du module régime.

Vérifie que les colonnes de régime sont présentes et cohérentes,
et que la structure existante (cols numériques, highlights) est préservée.
"""

from __future__ import annotations

import trader.agent_context as agent_context
import trader.regime as regime
from trader.agent_context import build_market_cockpit
from trader.tools.market import Bar


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

    assert cols[-3:] == ["htf", "aligned", "sig"]
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
