"""Tests TDD pour build_market_cockpit — intégration du module régime.

Vérifie que les colonnes de régime sont présentes et cohérentes,
et que la structure existante (cols numériques, highlights) est préservée.
"""

from __future__ import annotations

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


def test_cockpit_version_est_cp2() -> None:
    """La version du cockpit est mise à jour à cp2 suite à l'ajout du régime."""
    bars = {"SPY": _trending_bars()}
    result = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 105.0},
        window=12,
    )
    assert result["v"] == "cp2"


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
