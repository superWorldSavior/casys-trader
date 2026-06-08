"""tests pour trader.stats — vérifie compute_live_kpis sans réseau ni daemon."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from trader.stats import compute_live_kpis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_state(
    tmp_path: Path,
    *,
    history_rows: list[dict],
    broker: dict,
    starting_cash: float = 100_000.0,
    model_performance_rows: list[dict] | None = None,
) -> Path:
    """Crée l'arborescence attendue par compute_live_kpis dans tmp_path.

    Layout : tmp_path/
                config/universe.yaml
                state/
                    history.jsonl
                    broker.json

    Retourne le répertoire state/.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": starting_cash, "symbols": ["AAPL"]}),
        encoding="utf-8",
    )

    if history_rows:
        with (state_dir / "history.jsonl").open("w", encoding="utf-8") as fh:
            for row in history_rows:
                fh.write(json.dumps(row) + "\n")

    (state_dir / "broker.json").write_text(json.dumps(broker), encoding="utf-8")
    if model_performance_rows:
        with (state_dir / "model_performance.jsonl").open("w", encoding="utf-8") as fh:
            for row in model_performance_rows:
                fh.write(json.dumps(row) + "\n")

    return state_dir


# ---------------------------------------------------------------------------
# Tests principaux
# ---------------------------------------------------------------------------


def test_compute_live_kpis_retourne_toutes_les_cles_attendues(tmp_path: Path) -> None:
    """Le dict retourné contient exactement les clés spécifiées."""
    state_dir = _setup_state(
        tmp_path,
        history_rows=[
            {"ts": "2026-01-01T10:00:00", "equity": 100_000.0, "cash": 100_000.0, "n_decisions": 0, "n_executed": 0},
            {"ts": "2026-01-01T11:00:00", "equity": 102_000.0, "cash": 95_000.0, "n_decisions": 1, "n_executed": 1},
        ],
        broker={"cash": 95_000.0, "positions": {}, "fills": [{"symbol": "AAPL", "side": "BUY", "quantity": 10, "price": 200.0, "ts": "2026-01-01T11:00:00"}]},
    )

    kpis = compute_live_kpis(state_dir)

    cles_attendues = {"equity", "cash", "total_return", "max_drawdown", "period_win_rate", "volatility", "sharpe", "num_trades", "n_positions", "positions", "model_performance"}
    assert set(kpis.keys()) == cles_attendues


def test_compute_live_kpis_total_return_coherent(tmp_path: Path) -> None:
    """total_return = (dernière_equity / starting_cash) - 1."""
    starting_cash = 100_000.0
    last_equity = 110_000.0

    state_dir = _setup_state(
        tmp_path,
        history_rows=[
            {"ts": "2026-01-01T10:00:00", "equity": 100_000.0, "cash": 100_000.0, "n_decisions": 0, "n_executed": 0},
            {"ts": "2026-01-02T10:00:00", "equity": 105_000.0, "cash": 95_000.0, "n_decisions": 2, "n_executed": 1},
            {"ts": "2026-01-03T10:00:00", "equity": last_equity, "cash": 90_000.0, "n_decisions": 1, "n_executed": 1},
        ],
        broker={"cash": 90_000.0, "positions": {}, "fills": []},
        starting_cash=starting_cash,
    )

    kpis = compute_live_kpis(state_dir)

    expected_return = last_equity / starting_cash - 1.0
    assert kpis["total_return"] == pytest.approx(expected_return)
    assert kpis["equity"] == pytest.approx(last_equity)


def test_compute_live_kpis_positions_filtrees(tmp_path: Path) -> None:
    """Seules les positions avec quantité non nulle apparaissent dans positions[]."""
    state_dir = _setup_state(
        tmp_path,
        history_rows=[
            {"ts": "2026-01-01T10:00:00", "equity": 100_000.0, "cash": 80_000.0, "n_decisions": 1, "n_executed": 1},
        ],
        broker={
            "cash": 80_000.0,
            "positions": {
                "AAPL": {"symbol": "AAPL", "quantity": 5.0, "avg_price": 180.0},
                "TSLA": {"symbol": "TSLA", "quantity": 0.0, "avg_price": 0.0},  # clôturée
            },
            "fills": [],
        },
    )

    kpis = compute_live_kpis(state_dir)

    assert kpis["n_positions"] == 1
    assert len(kpis["positions"]) == 1
    assert kpis["positions"][0]["symbol"] == "AAPL"


def test_compute_live_kpis_num_trades_depuis_fills(tmp_path: Path) -> None:
    """num_trades = nombre de fills dans broker.json."""
    state_dir = _setup_state(
        tmp_path,
        history_rows=[
            {"ts": "2026-01-01T10:00:00", "equity": 100_000.0, "cash": 100_000.0, "n_decisions": 0, "n_executed": 0},
            {"ts": "2026-01-02T10:00:00", "equity": 101_000.0, "cash": 91_000.0, "n_decisions": 3, "n_executed": 3},
        ],
        broker={
            "cash": 91_000.0,
            "positions": {},
            "fills": [
                {"symbol": "AAPL", "side": "BUY", "quantity": 10, "price": 190.0, "ts": "t1"},
                {"symbol": "AAPL", "side": "BUY", "quantity": 5, "price": 192.0, "ts": "t2"},
                {"symbol": "AAPL", "side": "SELL", "quantity": 15, "price": 195.0, "ts": "t3"},
            ],
        },
    )

    kpis = compute_live_kpis(state_dir)

    assert kpis["num_trades"] == 3


def test_compute_live_kpis_agrege_la_perf_par_modele(tmp_path: Path) -> None:
    state_dir = _setup_state(
        tmp_path,
        history_rows=[
            {"ts": "2026-01-01T10:00:00", "equity": 100_000.0, "cash": 100_000.0, "n_decisions": 0, "n_executed": 0},
        ],
        broker={"cash": 98_000.0, "positions": {}, "fills": []},
        model_performance_rows=[
            {
                "ts": "2026-01-01T10:00:00",
                "symbol": "SPY",
                "llm_provider": "spark",
                "llm_model": "gpt-5.3-codex-spark/medium",
                "confidence": 0.6,
                "equity": 100_000.0,
            },
            {
                "ts": "2026-01-01T11:00:00",
                "symbol": "QQQ",
                "llm_provider": "spark",
                "llm_model": "gpt-5.3-codex-spark/medium",
                "confidence": 0.8,
                "equity": 100_200.0,
            },
            {
                "ts": "2026-01-01T12:00:00",
                "symbol": "SPY",
                "llm_provider": "ollama-cloud",
                "llm_model": "nemotron-3-nano:30b-cloud",
                "llm_fallback_reason": "spark:quota_exceeded",
                "confidence": 0.7,
                "equity": 99_900.0,
            },
        ],
    )

    kpis = compute_live_kpis(state_dir)

    spark = next(row for row in kpis["model_performance"] if row["provider"] == "spark")
    ollama = next(row for row in kpis["model_performance"] if row["provider"] == "ollama-cloud")
    assert spark["fills"] == 2
    assert spark["symbols"] == ["QQQ", "SPY"]
    assert spark["portfolio_equity_delta"] == 200.0
    assert spark["avg_confidence"] == pytest.approx(0.7)
    assert ollama["fills"] == 1
    assert ollama["fallbacks"] == 1


# ---------------------------------------------------------------------------
# Test robustesse : fichiers absents
# ---------------------------------------------------------------------------


def test_compute_live_kpis_tolere_fichiers_absents(tmp_path: Path) -> None:
    """Quand history.jsonl, broker.json et universe.yaml sont absents, retourne des valeurs neutres sans lever."""
    # state_dir vide — aucun fichier présent
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    kpis = compute_live_kpis(state_dir)

    assert isinstance(kpis, dict)
    # Valeurs neutres attendues
    assert kpis["equity"] is None
    assert kpis["total_return"] == pytest.approx(0.0)
    assert kpis["max_drawdown"] == pytest.approx(0.0)
    assert kpis["num_trades"] == 0
    assert kpis["n_positions"] == 0
    assert kpis["positions"] == []


def test_compute_live_kpis_ignore_lignes_equity_null(tmp_path: Path) -> None:
    """Les lignes equity=null (kill-switch) sont ignorées dans la courbe."""
    state_dir = _setup_state(
        tmp_path,
        history_rows=[
            {"ts": "2026-01-01T10:00:00", "equity": 100_000.0, "cash": 100_000.0, "n_decisions": 0, "n_executed": 0},
            # cycle kill-switch : equity=null, cash=null
            {"ts": "2026-01-01T11:00:00", "equity": None, "cash": None, "n_decisions": 0, "n_executed": 0},
            {"ts": "2026-01-01T12:00:00", "equity": 103_000.0, "cash": 93_000.0, "n_decisions": 1, "n_executed": 1},
        ],
        broker={"cash": 93_000.0, "positions": {}, "fills": []},
        starting_cash=100_000.0,
    )

    kpis = compute_live_kpis(state_dir)

    # La courbe doit avoir 2 points (le null est ignoré)
    assert kpis["equity"] == pytest.approx(103_000.0)
    assert kpis["total_return"] == pytest.approx(0.03)
