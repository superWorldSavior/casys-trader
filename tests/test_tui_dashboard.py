"""Tests du dashboard TUI enrichi : sparkline, KPI, attribution, courbe d'équité."""

from __future__ import annotations

from rich.console import Console

from trader.tui import build_view, sparkline


def _render(renderable, width: int = 120) -> str:
    console = Console(width=width)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_sparkline_croissant_finit_au_max() -> None:
    s = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(s) == 8
    assert s[-1] == "█"
    assert s[0] == "▁"


def test_sparkline_vide_renvoie_chaine_vide() -> None:
    assert sparkline([]) == ""


def test_sparkline_constant_ne_plante_pas() -> None:
    s = sparkline([5.0, 5.0, 5.0])
    assert len(s) == 3


def test_build_view_affiche_les_kpi() -> None:
    state = {
        "portfolio": {"equity": 100_000.0, "cash": 100_000.0},
        "kpis": {
            "sharpe": 1.5,
            "max_drawdown": -0.08,
            "period_win_rate": 0.42,
            "volatility": 0.13,
            "num_trades": 7,
        },
    }
    out = _render(build_view(state))
    assert "Sharpe" in out
    assert "1.5" in out
    assert "Win" in out


def test_build_view_affiche_la_calibration_de_confidence() -> None:
    state = {
        "attribution": {
            "n_closed_trades": 4,
            "realized_pnl": -120.0,
            "win_rate": 0.25,
            "avg_pnl": -30.0,
            "avg_holding_minutes": 90.0,
            "by_confidence": [
                {"bucket": "0.85-1.0", "n": 3, "total_pnl": -150.0, "win_rate": 0.0, "avg_pnl": -50.0},
            ],
            "by_exit_reason": [
                {"reason": "hard_stop", "n": 2, "total_pnl": -100.0, "win_rate": 0.0, "avg_pnl": -50.0},
            ],
        },
    }
    out = _render(build_view(state))
    assert "Attribution" in out
    assert "0.85-1.0" in out
    assert "hard_stop" in out


def test_build_view_affiche_la_courbe_dequite_sans_planter() -> None:
    state = {
        "portfolio": {"equity": 101_000.0},
        "equity_curve": [100_000, 100_500, 99_800, 101_000, 102_000, 101_500],
    }
    out = _render(build_view(state))
    assert "quit" in out  # panneau "Équité"


def test_build_view_affiche_les_learnings_longs_en_entier() -> None:
    note = (
        "le range tient depuis cinq réveils consécutifs, j'attends une cassure nette "
        "au-dessus de la résistance avant d'ouvrir et je garde le scénario invalide "
        "tant que le volume ne confirme pas la sortie"
    )
    state = {
        "learnings": [
            {
                "ts": "2026-06-07T10:15:00+02:00",
                "symbol": "AAPL",
                "note": note,
            }
        ]
    }

    out = _render(build_view(state), width=120)
    compact_out = "".join(ch for ch in out if not ch.isspace() and ch != "│")
    compact_note = "".join(note.split())

    assert compact_note in compact_out
    assert "tantquelevolumeneconfirmepaslasortie" in compact_out
    assert "2026-06-07 08:15 UTC" in out
    assert "2026-06-07T10:15:00+02:00" not in out
    assert "..." not in out


def test_build_view_etat_minimal_ne_plante_pas() -> None:
    out = _render(build_view({}))
    assert out is not None
