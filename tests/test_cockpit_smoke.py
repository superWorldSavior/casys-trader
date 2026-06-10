"""Tests de fumée du cockpit Textual.

Vérifient que :
- L'app monte sans crash (panes existent dans le DOM)
- La touche q quitte proprement
- Les widgets d'état gèrent un fichier absent sans crash

Note : les fichiers state sont mockés via monkeypatch sur les constantes du module.
"""

from __future__ import annotations

import json
from pathlib import Path

import trader.cockpit as cockpit_module
from trader.cockpit import CockpitApp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_state(tmp_path: Path) -> None:
    """Crée un état minimal dans tmp_path pour que load_runtime_state réussisse."""
    (tmp_path / "current_report.json").write_text(
        json.dumps(
            {
                "ts": "2026-06-10T10:00:00+00:00",
                "dry_run": True,
                "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "daemon_status.json").write_text(
        json.dumps(
            {
                "phase": "idle",
                "decisions_done": 0,
                "symbols_total": 0,
                "model_calls_used": 0,
                "max_model_calls_per_cycle": 25,
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_cockpit_app_monte_avec_les_deux_panes(tmp_path, monkeypatch):
    """L'app Textual monte sans exception et les deux panneaux sont dans le DOM."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        assert app.query_one("#dashboard-pane") is not None
        assert app.query_one("#events-pane") is not None
        assert app.query_one("#cockpit-status") is not None


async def test_cockpit_touche_q_quitte(tmp_path, monkeypatch):
    """La touche q ferme l'app proprement."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("q")
    # Si on sort du context manager sans TimeoutError, le test passe.


async def test_cockpit_fichiers_absents_ne_crashent_pas(tmp_path, monkeypatch):
    """Aucun fichier d'état → l'app monte quand même (panes vides, pas de crash)."""
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        # Doit monter sans lever d'exception
        assert app.query_one("#dashboard-pane") is not None


async def test_cockpit_theme_defaut_est_casys_salmon(tmp_path, monkeypatch):
    """Le thème par défaut doit être casys-salmon (fond clair saumon)."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        assert app.theme == "casys-salmon"


async def test_cockpit_binding_d_bascule_theme(tmp_path, monkeypatch):
    """La touche d bascule entre casys-salmon et casys-ink."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        assert app.theme == "casys-salmon"
        await pilot.press("d")
        assert app.theme == "casys-ink"
        await pilot.press("d")
        assert app.theme == "casys-salmon"


async def test_cockpit_status_prend_palette_en_compte(tmp_path, monkeypatch):
    """Finding 3 : CockpitStatus.update_state reçoit la palette et l'utilise.

    Le widget doit exposer update_state(state, kill_active, palette=...).
    """
    from trader.cockpit import CockpitStatus
    from trader.palette import PALETTE_LIGHT, PALETTE_DARK

    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        status = app.query_one("#cockpit-status", CockpitStatus)
        state = {
            "portfolio": {
                "cash": 100000.0,
                "equity": 100000.0,
                "total_return_pct": 0.5,
            },
            "kpis": {},
            "daemon_status": {"phase": "idle"},
            "dry_run": True,
        }
        # Doit accepter palette= sans TypeError
        status.update_state(state, kill_active=False, palette=PALETTE_LIGHT)
        status.update_state(state, kill_active=False, palette=PALETTE_DARK)


async def test_cockpit_toggle_theme_propage_palette_dashboard_immediatement(
    tmp_path, monkeypatch
):
    """Finding 4 : après toggle d, le DashboardPane utilise la nouvelle palette
    dès le prochain update_state — pas besoin d'attendre le poll interval.

    On vérifie que _current_palette du dashboard change immédiatement après
    action_toggle_theme, sans attendre le prochain cycle de refresh.
    """
    from trader.cockpit import DashboardPane
    from trader.palette import PALETTE_DARK, PALETTE_LIGHT

    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        dashboard = app.query_one("#dashboard-pane", DashboardPane)
        # Thème saumon → palette LIGHT
        assert dashboard._current_palette is PALETTE_LIGHT
        # Toggle → palette DARK immédiatement
        await pilot.press("d")
        assert dashboard._current_palette is PALETTE_DARK
        # Retour → palette LIGHT
        await pilot.press("d")
        assert dashboard._current_palette is PALETTE_LIGHT
