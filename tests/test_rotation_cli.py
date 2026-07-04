"""Tests TDD pour run_cli() — câblage prod de la rotation, réseau injecté.

Toutes les dépendances réseau sont substituées par des fakes déterministes.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from trader.market.market_data import Bar
from trader.market.rotation.collectors import default_override_fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars(n: int = 20, *, base: float = 100.0, symbol: str = "X") -> list[Bar]:
    """Génère n barres daily avec une tendance haussière légère."""
    bars = []
    for i in range(n):
        close = base * (1 + 0.005 * i)
        bars.append(Bar(
            ts=f"2026-05-{i + 1:02d}" if i < 27 else f"2026-06-{i - 26:02d}",
            open=close * 0.999,
            high=close * 1.002,
            low=close * 0.998,
            close=close,
            volume=1_000_000.0,
        ))
    return bars


def _fake_fetch_fn(symbols: list[str]) -> dict[str, list[Bar]]:
    """Retourne 20 barres synthétiques pour chaque symbole demandé."""
    return {s: _make_bars(20, base=100.0 + hash(s) % 50, symbol=s) for s in symbols}


def _make_config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # pool.yaml minimal
    (config_dir / "pool.yaml").write_text(
        "symbols: [AAA, BBB]\nhard_exclusions: []\n",
        encoding="utf-8",
    )

    # radar.yaml minimal — tous les champs obligatoires
    radar_cfg = {
        "atr_floor": 0.0,
        "min_coverage": 0.0,
        "amplitude_cap": 0.05,
        "cap_m": 5,
        "benchmarks": {"US": "SPY"},
        "default_benchmark": "SPY",
        "score_window_bars": 15,
        "w_trend": 1.0,
        "w_rs": 1.0,
        "w_amp": 1.0,
        "delta": 0.01,
        "dwell_days": 1,
        "emergency_score": 0.0,
    }
    (config_dir / "radar.yaml").write_text(yaml.safe_dump(radar_cfg), encoding="utf-8")

    # conviction.yaml vide
    (config_dir / "conviction.yaml").write_text("{}\n", encoding="utf-8")

    return config_dir


# ---------------------------------------------------------------------------
# Test nominal
# ---------------------------------------------------------------------------

def test_run_cli_writes_expected_artifacts(tmp_path):
    """run_cli avec fetch_fn injecté écrit universe.yaml, radar_snapshot.json et rotation_state.json.

    Vérifie :
    - result["written"] is True
    - config_dir/universe.yaml existe et contient des symboles
    - state_dir/radar_snapshot.json existe
    - state_dir/rotation_state.json existe
    """
    from trader.market.rotation.wiring import run_cli

    config_dir = _make_config_dir(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    result = run_cli(
        config_dir,
        state_dir,
        fetch_fn=_fake_fetch_fn,
        sticky_fn=lambda: set(),
        override_fn=default_override_fn,
        as_of="2026-06-15",
    )

    # 1. résultat cohérent
    assert result["written"] is True, f"written devrait être True, got: {result}"

    # 2. universe.yaml écrit avec des symboles
    universe_path = config_dir / "universe.yaml"
    assert universe_path.exists(), "config_dir/universe.yaml doit exister"
    universe_content = yaml.safe_load(universe_path.read_text(encoding="utf-8"))
    assert isinstance(universe_content, dict), "universe.yaml doit être un dict"
    assert "symbols" in universe_content, "universe.yaml doit contenir 'symbols'"
    assert len(universe_content["symbols"]) > 0, "symbols ne doit pas être vide"

    # 3. radar_snapshot.json écrit
    snapshot_path = state_dir / "radar_snapshot.json"
    assert snapshot_path.exists(), "state_dir/radar_snapshot.json doit exister"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert "as_of" in snapshot, "snapshot doit contenir 'as_of'"

    # 4. rotation_state.json écrit
    state_path = state_dir / "rotation_state.json"
    assert state_path.exists(), "state_dir/rotation_state.json doit exister"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "current_hot_set" in state, "rotation_state.json doit contenir 'current_hot_set'"
