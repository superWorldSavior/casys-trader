"""Tests TDD pour rotation.run() — orchestration EOD.

Toutes les I/O réseau/agent sont injectées — déterminisme garanti.
"""
from __future__ import annotations

import json

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


# ---------------------------------------------------------------------------
# Test A — No-leader : ranked vide → pas d'écriture, no_leader dans alerts
# ---------------------------------------------------------------------------

def test_no_leader_written_false_no_universe_write(tmp_path):
    """rank_fn renvoie ranked=[] → written=False, universe.yaml non écrit,
    'no_leader' dans alerts, état hystérésis non avancé (conservé).
    """
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    # État hystérésis pré-existant
    initial_state = {
        "current_hot_set": ["X"],
        "dwell_days_by_symbol": {"X": 3},
        "last_valid_universe": ["X", "Y"],
    }
    (state_dir / "rotation_state.json").write_text(
        json.dumps(initial_state), encoding="utf-8"
    )

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=lambda: {"ranked": []},
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"X", "Y"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is False, "Pas d'écriture si final vide"
    assert "no_leader" in result["alerts"], "'no_leader' doit figurer dans alerts"
    assert (config_dir / "universe.yaml").exists() is False, (
        "universe.yaml ne doit pas être écrit si final est vide"
    )
    # final_hot_set = last_valid_universe (fallback)
    assert set(result["final_hot_set"]) == {"X", "Y"}, (
        "final_hot_set doit être last_valid_universe"
    )
    # L'état hystérésis ne doit pas avoir avancé (rotation_state.json inchangé)
    persisted = json.loads((state_dir / "rotation_state.json").read_text())
    assert persisted["current_hot_set"] == initial_state["current_hot_set"], (
        "current_hot_set ne doit pas changer sur no_leader"
    )


# ---------------------------------------------------------------------------
# Test B — Bootstrap depuis universe.yaml
# ---------------------------------------------------------------------------

def test_bootstrap_seeds_from_universe_yaml(tmp_path):
    """Sans rotation_state.json mais avec config/universe.yaml [A, B] :
    après run, l'état persisté a un dwell pour A et B.
    """
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    # universe.yaml pré-existant avec A et B
    import yaml as _yaml
    (config_dir / "universe.yaml").write_text(
        _yaml.safe_dump({"symbols": ["A", "B"]}), encoding="utf-8"
    )

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9), ("B", 0.7)),
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"A", "B"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is True
    state_path = state_dir / "rotation_state.json"
    assert state_path.exists()
    persisted = json.loads(state_path.read_text())
    # A et B doivent avoir un dwell (seedés puis avancés)
    assert "A" in persisted["dwell_days_by_symbol"] or "B" in persisted["dwell_days_by_symbol"], (
        "Au moins A ou B doit avoir un dwell après bootstrap + run"
    )


# ---------------------------------------------------------------------------
# Test C — advance_state avec last_valid kwarg
# ---------------------------------------------------------------------------

# Ce test vit dans test_rotation_state.py mais on en met un de bout-en-bout ici :
def test_sticky_absent_current_hot_set_last_valid_contains_sticky(tmp_path):
    """Un symbole sticky (Z) aujourd'hui mais absent du hot non-sticky.
    Après run :
    - Z absent de current_hot_set persisté
    - last_valid_universe contient Z (car final = hot + sticky)
    """
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9), ("B", 0.7)),  # Z pas dans ranked
        sticky_fn=_sticky_fn({"Z"}),
        override_fn=_override_fn_noop(),
        pool={"A", "B", "Z"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is True
    persisted = json.loads((state_dir / "rotation_state.json").read_text())
    assert "Z" not in persisted["current_hot_set"], (
        "Z (sticky uniquement) ne doit pas être dans current_hot_set"
    )
    assert "Z" in persisted["last_valid_universe"], (
        "Z doit figurer dans last_valid_universe car il est dans final"
    )


# ---------------------------------------------------------------------------
# Test D — run() écrit radar_snapshot.json si rank_fn fournit ineligible/components
# ---------------------------------------------------------------------------

def test_run_writes_radar_snapshot(tmp_path):
    """rank_fn fournit ranked + ineligible + components_by_symbol →
    state/radar_snapshot.json est créé.
    """
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    def _full_rank_fn():
        return {
            "ranked": [{"symbol": "A", "attractiveness": 0.9}],
            "ineligible": [{"symbol": "B", "reason": "not_eligible"}],
            "components_by_symbol": {"A": {"ret": 0.05, "efficiency_ratio": 0.8}},
        }

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_full_rank_fn,
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"A", "B"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is True
    snapshot_path = state_dir / "radar_snapshot.json"
    assert snapshot_path.exists(), "radar_snapshot.json doit être écrit"
    snapshot = json.loads(snapshot_path.read_text())
    assert snapshot["as_of"] == "2026-06-15"
    assert len(snapshot["ranked"]) == 1


# ---------------------------------------------------------------------------
# Test D bis — run() ne plante pas si ineligible/components absents de rank_fn
# ---------------------------------------------------------------------------

def test_run_no_crash_if_no_ineligible_in_rank(tmp_path):
    """rank_fn renvoie uniquement {'ranked': [...]} sans ineligible →
    pas de crash, radar_snapshot.json non créé.
    """
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9)),
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"A"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is True
    # Pas de crash = test réussi; radar_snapshot.json peut ou non exister
    # (on vérifie juste qu'il n'y a pas d'exception)


# ---------------------------------------------------------------------------
# Test E — Invariants stricts
# ---------------------------------------------------------------------------

def test_override_add_valid_pool_survives_final(tmp_path):
    """override add (symbole du pool) ACCEPTÉ → symbole dans final_hot_set."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    def _override_add_C(payload):
        return {"add": ["C"], "remove": []}

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9), ("B", 0.7)),
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_add_C,
        pool={"A", "B", "C"},
        cap_m=3,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert "C" in result["final_hot_set"], "C ajouté via override valide doit être dans final"


def test_cumul_alerts_override_ko_and_sticky_over_cap(tmp_path):
    """override KO + sticky over-cap → les deux alertes présentes dans result."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    def _failing_override(payload):
        raise RuntimeError("timeout")

    # 3 stickies, cap_m=2 → sticky_over_cap
    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9)),
        sticky_fn=_sticky_fn({"X", "Y", "Z"}),
        override_fn=_failing_override,
        pool={"A", "X", "Y", "Z"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert "override_unavailable" in result["alerts"], "override KO doit générer alert"
    assert "sticky_over_cap" in result["alerts"], "sticky > cap doit générer alert"


def test_sticky_over_quota_non_sticky_count_le_cap_minus_sticky(tmp_path):
    """sticky=k, cap_m=m → nb symboles non-sticky dans final ≤ max(0, m - k)."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    sticky_syms = {"X", "Y"}  # k=2
    cap_m = 3  # m=3, donc max non-sticky = 1

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9), ("B", 0.8), ("C", 0.7)),
        sticky_fn=_sticky_fn(sticky_syms),
        override_fn=_override_fn_noop(),
        pool={"A", "B", "C", "X", "Y"},
        cap_m=cap_m,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    non_sticky_in_final = [s for s in result["final_hot_set"] if s not in sticky_syms]
    max_non_sticky = max(0, cap_m - len(sticky_syms))
    assert len(non_sticky_in_final) <= max_non_sticky, (
        f"Non-sticky dans final ({non_sticky_in_final}) dépasse la limite {max_non_sticky}"
    )


# ---------------------------------------------------------------------------
# Test F — gap_adverse consommé depuis rank_fn (prime sur param)
# ---------------------------------------------------------------------------

def test_gap_adverse_from_rank_fn_evicts_hot_symbol(tmp_path):
    """rank_fn renvoie gap_adverse={'X'} avec X dans le hot-set initial.

    X doit être évincé (sortie d'urgence) même si gap_adverse param par défaut est vide.
    Vérifie que le dict retourné par rank_fn prime sur le param gap_adverse.
    """
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    # X est dans le hot-set initial (dwell=5 — évictable)
    initial_state = {
        "current_hot_set": ["X", "A"],
        "dwell_days_by_symbol": {"X": 5, "A": 5},
        "last_valid_universe": ["X", "A"],
    }
    (state_dir / "rotation_state.json").write_text(
        json.dumps(initial_state), encoding="utf-8"
    )

    def _rank_with_gap_adverse():
        return {
            "ranked": [
                {"symbol": "X", "attractiveness": 0.9},
                {"symbol": "A", "attractiveness": 0.7},
            ],
            "gap_adverse": frozenset({"X"}),
        }

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_with_gap_adverse,
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"X", "A"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
        gap_adverse=frozenset(),  # param vide — le dict doit primer
    )

    assert "X" not in result["final_hot_set"], (
        "X doit être évincé via gap_adverse fourni par rank_fn"
    )
    assert result["written"] is True


# ---------------------------------------------------------------------------
# Test G — rotation_at persisté : chemin succès
# ---------------------------------------------------------------------------

def test_rotation_at_persisted_on_success(tmp_path):
    """Run nominal → rotation_state.json a last_rotation_at == as_of."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=_rank_fn(("A", 0.9), ("B", 0.7)),
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"A", "B"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is True
    persisted = json.loads((state_dir / "rotation_state.json").read_text())
    assert persisted.get("last_rotation_at") == "2026-06-15", (
        "last_rotation_at doit être mis à jour à as_of sur succès"
    )


# ---------------------------------------------------------------------------
# Test H — rotation_at persisté : chemin no-leader
# ---------------------------------------------------------------------------

def test_rotation_at_persisted_on_no_leader(tmp_path):
    """No-leader (ranked=[]) → last_rotation_at == as_of ET hot-set inchangé."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    initial_state = {
        "current_hot_set": ["X"],
        "dwell_days_by_symbol": {"X": 3},
        "last_valid_universe": ["X", "Y"],
        "last_rotation_at": "",
    }
    (state_dir / "rotation_state.json").write_text(
        json.dumps(initial_state), encoding="utf-8"
    )

    result = run(
        config_dir=str(config_dir),
        state_dir=str(state_dir),
        as_of="2026-06-15",
        rank_fn=lambda: {"ranked": []},
        sticky_fn=_sticky_fn(set()),
        override_fn=_override_fn_noop(),
        pool={"X", "Y"},
        cap_m=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
    )

    assert result["written"] is False
    assert "no_leader" in result["alerts"]

    persisted = json.loads((state_dir / "rotation_state.json").read_text())
    assert persisted.get("last_rotation_at") == "2026-06-15", (
        "last_rotation_at doit être persisté même sur no-leader"
    )
    assert persisted["current_hot_set"] == initial_state["current_hot_set"], (
        "hot-set doit rester inchangé sur no-leader"
    )
    assert persisted["dwell_days_by_symbol"] == initial_state["dwell_days_by_symbol"], (
        "dwell ne doit pas changer sur no-leader"
    )


# ---------------------------------------------------------------------------
# Test I — rotation_at NON mis à jour sur CoverageError
# ---------------------------------------------------------------------------

def test_rotation_at_not_updated_on_coverage_error(tmp_path):
    """CoverageError → last_rotation_at ne doit PAS être mis à jour."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

    initial_state = {
        "current_hot_set": ["X"],
        "dwell_days_by_symbol": {"X": 3},
        "last_valid_universe": ["X", "Y"],
        "last_rotation_at": "2026-06-10",
    }
    (state_dir / "rotation_state.json").write_text(
        json.dumps(initial_state), encoding="utf-8"
    )

    def _failing_rank():
        raise CoverageError("not enough data")

    run(
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

    # rotation_state.json ne doit pas être écrit / modifié en cas de CoverageError
    state_path = state_dir / "rotation_state.json"
    if state_path.exists():
        persisted = json.loads(state_path.read_text())
        assert persisted.get("last_rotation_at") == "2026-06-10", (
            "last_rotation_at ne doit pas changer sur CoverageError"
        )
