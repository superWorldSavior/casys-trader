"""Interactions de la home : DataTables, focus, modal symbole, filtres."""
from __future__ import annotations

import json
from pathlib import Path


def _patch_state_paths(monkeypatch, tmp_path: Path) -> None:
    """Même mécanique que tests/test_cockpit_smoke.py : monkeypatch des constantes.

    Supprime aussi ConfirmStart (via _maybe_propose_start=noop) pour que les tests
    d'interaction claviers/focus ne soient pas bloqués par la modal au démarrage.
    """
    import trader.cockpit.app as cockpit_module

    (tmp_path / "current_report.json").write_text(json.dumps({
        "ts": "2026-07-03T10:00:00+00:00",
        "dry_run": True,
        "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": [
            {"symbol": "AAA.TW", "quantity": 10, "avg_price": 90.0, "last_price": 100.0},
        ]},
        "decisions": [{"symbol": "AAA.TW", "action": "BUY", "executed": True,
                       "confidence": 0.7, "ts": "2026-07-03T09:59:00Z"}],
    }), encoding="utf-8")
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")
    import trader.read_models.runtime_state as rs
    monkeypatch.setattr(rs, "_STATE_DIR", tmp_path)
    # Supprime la modal ConfirmStart : elle intercepte les touches et le focus
    # ce qui casse les tests de navigation/focus clavier.
    monkeypatch.setattr(cockpit_module.CockpitApp, "_maybe_propose_start", lambda self: None)


async def test_positions_table_vide_affiche_placeholder(tmp_path, monkeypatch):
    from trader.cockpit.home import PositionsTable
    from trader.cockpit import CockpitApp
    from trader.ui.palette import PALETTE_LIGHT

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        table = app.query_one(PositionsTable)
        table.refresh_rows({}, palette=PALETTE_LIGHT)
        assert table.row_count == 1  # ligne placeholder "—"


async def test_decisions_table_montee_et_peuplee(tmp_path, monkeypatch):
    from trader.cockpit.home import DecisionsTable
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app._schedule_refresh_state()
        await pilot.pause(1.0)
        table = app.query_one(DecisionsTable)
        assert table.row_count >= 1


async def test_enter_ouvre_modal_et_esc_ferme(tmp_path, monkeypatch):
    from trader.cockpit.home import DecisionsTable, SymbolDetailScreen
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app._schedule_refresh_state()
        await pilot.pause(1.0)
        table = app.query_one(DecisionsTable)
        table.focus()
        await pilot.press("enter")
        assert isinstance(app.screen, SymbolDetailScreen)
        await pilot.press("escape")
        assert not isinstance(app.screen, SymbolDetailScreen)


async def test_pages_par_chiffres_tab_ne_change_plus_de_page(tmp_path, monkeypatch):
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        assert app._active_page_key == "decisions"
        await pilot.press("tab")
        assert app._active_page_key == "decisions"  # tab = focus, plus de changement de page


async def test_tab_focus_une_datatable(tmp_path, monkeypatch):
    """Tab (libéré des pages) donne le focus à une DataTable de la home."""
    from textual.widgets import DataTable
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        assert isinstance(app.focused, DataTable)


async def test_flux_filtre_regex_et_classes(tmp_path, monkeypatch):
    import json

    from textual.widgets import RichLog
    from trader.cockpit.events import EventClass
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    events = tmp_path / "events.jsonl"
    lines = [
        {"ts": "2026-07-03T01:00:00Z", "event": "decision_recorded", "symbol": "AAA.TW",
         "action": "BUY", "executed": True},
        {"ts": "2026-07-03T01:00:01Z", "event": "decision_recorded", "symbol": "BBB.TW",
         "action": "HOLD"},
        {"ts": "2026-07-03T01:00:02Z", "event": "cycle_completed", "decisions_done": 0},
    ]
    events.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        # Navigate to logs page so the RichLog gets its layout (→ _size_known=True)
        # and deferred backlog writes are flushed into RichLog.lines.
        await pilot.press("6")
        await pilot.pause()
        pane = app.query_one("#logs-pane")
        pane.poll_events(events)
        base_count = len(pane.query_one(RichLog).lines)

        pane.set_filters(None, "AAA")
        assert len(pane.query_one(RichLog).lines) < base_count

        pane.set_filters({EventClass.DECISION_EXECUTED}, None)
        rendered_count = len(pane.query_one(RichLog).lines)
        assert rendered_count >= 1  # le BUY exécuté passe

        pane.set_filters(None, None)  # reset
        assert len(pane.query_one(RichLog).lines) >= base_count - 1


async def test_binding_slash_ouvre_regex_modal(tmp_path, monkeypatch):
    from trader.cockpit.app import RegexModal
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        assert isinstance(app.screen, RegexModal)


async def test_binding_F_ouvre_class_filter_modal(tmp_path, monkeypatch):
    from trader.cockpit.app import ClassFilterModal
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        assert isinstance(app.screen, ClassFilterModal)
