"""Tests TDD pour rotation.run() — orchestration EOD.

Toutes les I/O réseau/agent sont injectées — déterminisme garanti.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from trader.radar_data import CoverageError
from trader.rotation import run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rank_fn(*symbols_with_attr: tuple[str, float]):
    """Renvoie une rank_fn qui retourne ranked liste triée desc attractivité."""
    ranked = [
        {"symbol": s, "attractiveness": a}
        for s, a in sorted(symbols_with_attr, key=lambda x: -x[1])
    ]
    return lambda: {"ranked": ranked}


def _sticky_fn(symbols: set[str]):
    return lambda: symbols


def _override_fn_noop():
    return lambda payload: {"add": [], "remove": []}


# ---------------------------------------------------------------------------
# Test 1 — Nominal
# ---------------------------------------------------------------------------

def test_nominal_writes_universe_and_state(tmp_path):
    """Run nominal : 3 symboles, sticky Z, override noop, cap_m=2.

    Vérifie :
    - config/universe.yaml écrit et contient Z (sticky)
    - state/rotation_ledger.jsonl existe
    - state/rotation_state.json existe
    - retour written=True
    """
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9), ("B", 0.7), ("Z", 0.5)),
        sticky_fn=_sticky_fn({"Z"}),
        override_fn=_override_fn_noop(),
        pool={"A", "B", "Z"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is True

    universe_path = config_dir / "universe.yaml"
    assert universe_path.exists(), "universe.yaml doit être écrit"
    content = yaml.safe_load(universe_path.read_text())
    assert "Z" in content["symbols"], "Z (sticky) doit figurer dans l'univers"

    ledger_path = state_dir / "rotation_ledger.jsonl"
    assert ledger_path.exists(), "rotation_ledger.jsonl doit exister"

    state_path = state_dir / "rotation_state.json"
    assert state_path.exists(), "rotation_state.json doit exister"


# ---------------------------------------------------------------------------
# Test 2 — Override KO (TimeoutError)
# ---------------------------------------------------------------------------

def test_override_ko_alerts_and_no_crash(tmp_path):
    """override_fn lève TimeoutError → alerts contient 'override_unavailable', pas de crash."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    def _failing_override(payload):
        raise TimeoutError("agent timeout")

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9), ("B", 0.7)),
        sticky_fn=_sticky_fn(set()),
        override_fn=_failing_override,
        pool={"A", "B"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert "override_unavailable" in result["alerts"]
    # final_hot_set doit être le défaut (pas vide, pas de crash)
    assert isinstance(result["final_hot_set"], list)
    assert result["written"] is True


# ---------------------------------------------------------------------------
# Test 3 — CoverageError : fail-safe, pas d'écriture
# ---------------------------------------------------------------------------

def test_coverage_error_failsafe(tmp_path):
    """rank_fn lève CoverageError → written=False, universe.yaml NON réécrit."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    # Pré-existant : last_valid_universe = ["X", "Y"]
    initial_state = {
        "current_hot_set": ["X"],
        "dwell_days_by_symbol": {"X": 3},
        "last_valid_universe": ["X", "Y"],
    }
    (state_dir / "rotation_state.json").write_text(
        json.dumps(initial_state), encoding="utf-8"
    )

    def _failing_rank():
        raise CoverageError("not enough data")

    universe_path = config_dir / "universe.yaml"

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_failing_rank,
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"X", "Y"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is False
    assert set(result["final_hot_set"]) == {"X", "Y"}, (
        "final_hot_set doit être last_valid_universe"
    )
    assert result["alerts"] == ["coverage_insufficient"]
    assert not universe_path.exists(), "universe.yaml ne doit PAS être écrit en cas de CoverageError"


# ---------------------------------------------------------------------------
# Test 4 — Bootstrap (pas de rotation_state.json)
# ---------------------------------------------------------------------------

def test_bootstrap_no_prior_state(tmp_path):
    """Sans rotation_state.json : current vide → hot-set calculé depuis le ranking.

    Vérifie que l'univers est écrit et l'état créé.
    """
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    # Pas de rotation_state.json → load_rotation_state retourne le défaut (current=[])

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9), ("B", 0.7), ("C", 0.5)),
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"A", "B", "C"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is True
    universe_path = config_dir / "universe.yaml"
    assert universe_path.exists(), "universe.yaml doit être créé au bootstrap"
    content = yaml.safe_load(universe_path.read_text())
    assert len(content["symbols"]) > 0, "hot-set non vide au bootstrap"

    state_path = state_dir / "rotation_state.json"
    assert state_path.exists(), "rotation_state.json doit être créé"
    state = json.loads(state_path.read_text())
    assert len(state["current_hot_set"]) > 0
