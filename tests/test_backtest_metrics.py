import pytest

from backtest.metrics import Metrics, compute_metrics, render_cli


def test_compute_metrics_calcule_les_kpi_sur_une_courbe_connue() -> None:
    curve = [
        ("2026-01-01T10:00:00", 100.0),
        ("2026-01-01T11:00:00", 110.0),
        ("2026-01-01T12:00:00", 88.0),
        ("2026-01-01T13:00:00", 99.0),
        ("2026-01-01T14:00:00", 120.0),
    ]
    trades = [{"symbol": "AAPL"}, {"symbol": "MSFT"}]

    metrics = compute_metrics(curve, trades, starting_equity=100.0)

    assert metrics.total_return == pytest.approx(0.2)
    assert metrics.max_drawdown == pytest.approx(0.2)
    assert metrics.period_win_rate == pytest.approx(3 / 4)
    assert metrics.num_trades == 2


def test_compute_metrics_retourne_des_metriques_neutres_sur_courbe_vide() -> None:
    metrics = compute_metrics([], [{"symbol": "AAPL"}], starting_equity=100.0)

    assert metrics == Metrics(
        total_return=0.0,
        max_drawdown=0.0,
        period_win_rate=0.0,
        volatility=0.0,
        sharpe=None,
        num_trades=1,
    )


def test_compute_metrics_retourne_des_metriques_neutres_sur_un_point() -> None:
    metrics = compute_metrics([("2026-01-01T10:00:00", 100.0)], [], starting_equity=100.0)

    assert metrics == Metrics(
        total_return=0.0,
        max_drawdown=0.0,
        period_win_rate=0.0,
        volatility=0.0,
        sharpe=None,
        num_trades=0,
    )


def test_compute_metrics_met_sharpe_a_none_quand_equite_plate() -> None:
    metrics = compute_metrics(
        [
            ("2026-01-01T10:00:00", 100.0),
            ("2026-01-01T11:00:00", 100.0),
            ("2026-01-01T12:00:00", 100.0),
        ],
        [],
        starting_equity=100.0,
    )

    assert metrics.volatility == 0.0
    assert metrics.sharpe is None


def test_render_cli_retourne_une_chaine_multiligne_lisible(capsys: pytest.CaptureFixture[str]) -> None:
    metrics = Metrics(
        total_return=0.1234,
        max_drawdown=0.0567,
        period_win_rate=0.75,
        volatility=0.0123,
        sharpe=1.2345,
        num_trades=3,
    )

    rendered = render_cli(metrics)

    assert "Rendement total : 12.34%" in rendered
    assert "Drawdown max    : 5.67%" in rendered
    assert "Sharpe          : 1.23" in rendered
    assert "Trades          : 3" in rendered
    assert "\n" in rendered
    assert capsys.readouterr().out == ""
