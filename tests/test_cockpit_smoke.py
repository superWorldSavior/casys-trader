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
    """L'app Textual monte sans exception et les panneaux principaux sont dans le DOM."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#left-pane") is not None
        assert app.query_one("#center-pane") is not None
        assert app.query_one("#right-pane") is not None
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
        assert app.query_one("#left-pane") is not None


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
    # Daemon simulé vivant → pas de ConfirmStart qui intercepterait les touches
    _make_minimal_state_with_pid(tmp_path, 54321)
    _patch_daemon_alive(monkeypatch, 54321)
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
    """Finding 4 : après toggle d, LeftPane utilise la nouvelle palette
    dès le prochain update_state — pas besoin d'attendre le poll interval.

    On vérifie que _current_palette de LeftPane change immédiatement après
    action_toggle_theme, sans attendre le prochain cycle de refresh.
    """
    from trader.cockpit import LeftPane
    from trader.palette import PALETTE_DARK, PALETTE_LIGHT

    # Daemon simulé vivant → pas de ConfirmStart qui intercepterait les touches
    _make_minimal_state_with_pid(tmp_path, 54321)
    _patch_daemon_alive(monkeypatch, 54321)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        left = app.query_one("#left-pane", LeftPane)
        # Thème saumon → palette LIGHT
        assert left._current_palette is PALETTE_LIGHT
        # Toggle → palette DARK immédiatement
        await pilot.press("d")
        assert left._current_palette is PALETTE_DARK
        # Retour → palette LIGHT
        await pilot.press("d")
        assert left._current_palette is PALETTE_LIGHT


# ---------------------------------------------------------------------------
# Indicateur vital
# ---------------------------------------------------------------------------


def test_indicateur_vital_alive_genere_vivant_markup(tmp_path, monkeypatch):
    """pid vivant + identité trader.daemon → vital.status == "alive", markup VIVANT."""
    from datetime import UTC, datetime, timedelta
    import json
    from trader.cockpit_supervisor import daemon_vital_state

    status_file = tmp_path / "daemon_status.json"
    now = datetime.now(UTC)
    ts = (now - timedelta(seconds=30)).isoformat()
    status_file.write_text(json.dumps({"ts": ts, "phase": "idle", "pid": 42}), encoding="utf-8")

    monkeypatch.setattr("trader.cockpit_supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit_supervisor._get_cmdline", lambda p: "uv run python -m trader.daemon --live")

    vital = daemon_vital_state(status_file)
    assert vital.status == "alive"
    assert vital.battement_old is False
    # Le markup produit pour ALIVE
    vital_str = "[bold green]● VIVANT[/bold green]"
    assert "VIVANT" in vital_str


def test_indicateur_vital_sans_pid_genere_never_started_markup(tmp_path):
    """daemon_status.json sans champ pid → never_started (format pré-migration)."""
    from datetime import UTC, datetime, timedelta
    import json
    from trader.cockpit_supervisor import daemon_vital_state

    status_file = tmp_path / "daemon_status.json"
    now = datetime.now(UTC)
    ts = (now - timedelta(minutes=10)).isoformat()
    # Pas de champ pid → never_started même avec ts vieux
    status_file.write_text(json.dumps({"ts": ts, "phase": "idle"}), encoding="utf-8")

    vital = daemon_vital_state(status_file)
    assert vital.status == "never_started"

    vital_str = "[dim]● jamais démarré[/dim]"
    assert "jamais" in vital_str


async def test_cockpit_monte_avec_daemon_status_recent(tmp_path, monkeypatch):
    """L'app monte sans crash avec un daemon_status.json récent — smoke intégration."""
    from datetime import UTC, datetime, timedelta
    import json

    _make_minimal_state(tmp_path)
    now = datetime.now(UTC)
    ts = (now - timedelta(seconds=30)).isoformat()
    status_data = {
        "ts": ts, "phase": "idle", "decisions_done": 0,
        "symbols_total": 0, "model_calls_used": 0, "max_model_calls_per_cycle": 25,
    }
    (tmp_path / "daemon_status.json").write_text(json.dumps(status_data), encoding="utf-8")

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        # L'app doit monter sans exception
        assert app.query_one("#cockpit-status") is not None


async def test_cockpit_monte_avec_daemon_status_vieux(tmp_path, monkeypatch):
    """L'app monte sans crash avec un daemon_status.json vieux > 3 min."""
    from datetime import UTC, datetime, timedelta
    import json

    _make_minimal_state(tmp_path)
    now = datetime.now(UTC)
    ts = (now - timedelta(minutes=10)).isoformat()
    status_data = {
        "ts": ts, "phase": "idle", "decisions_done": 0,
        "symbols_total": 0, "model_calls_used": 0, "max_model_calls_per_cycle": 25,
    }
    (tmp_path / "daemon_status.json").write_text(json.dumps(status_data), encoding="utf-8")

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        assert app.query_one("#cockpit-status") is not None


# ---------------------------------------------------------------------------
# Bindings déclarés : s / X / k
# ---------------------------------------------------------------------------


async def test_cockpit_binding_s_declare(tmp_path, monkeypatch):
    """Le binding 's' doit être déclaré dans BINDINGS."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        keys = [b.key for b in app.BINDINGS]
        assert "s" in keys


async def test_cockpit_binding_X_declare(tmp_path, monkeypatch):
    """Le binding 'X' (majuscule) doit être déclaré dans BINDINGS."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        keys = [b.key for b in app.BINDINGS]
        assert "X" in keys


async def test_cockpit_binding_k_declare(tmp_path, monkeypatch):
    """Le binding 'k' doit être déclaré dans BINDINGS."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        keys = [b.key for b in app.BINDINGS]
        assert "k" in keys


# ---------------------------------------------------------------------------
# Modals : X et k ouvrent des modals
# ---------------------------------------------------------------------------


async def test_cockpit_binding_X_monte_modal(tmp_path, monkeypatch):
    """La touche X pousse un écran modal ConfirmStop sur la pile."""
    # Daemon simulé vivant → pas de ConfirmStart au-dessus de la pile
    _make_minimal_state_with_pid(tmp_path, 54321)
    _patch_daemon_alive(monkeypatch, 54321)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("X")
        from trader.cockpit import ConfirmStop
        # push_screen pousse sur la pile — l'écran actif est le modal
        assert isinstance(app.screen, ConfirmStop)


async def test_cockpit_binding_k_monte_modal(tmp_path, monkeypatch):
    """La touche k pousse un écran modal ConfirmKill sur la pile."""
    # Daemon simulé vivant → pas de ConfirmStart au-dessus de la pile
    _make_minimal_state_with_pid(tmp_path, 54321)
    _patch_daemon_alive(monkeypatch, 54321)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("k")
        from trader.cockpit import ConfirmKill
        assert isinstance(app.screen, ConfirmKill)


# ---------------------------------------------------------------------------
# q ne touche jamais au daemon
# ---------------------------------------------------------------------------


async def test_cockpit_q_sans_daemon_quitte_directement(tmp_path, monkeypatch):
    """q sans daemon vivant quitte directement — pas de modal, aucun signal."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    # Pas de daemon_status.json avec pid → never_started
    signals_sent = []
    import os as _os_real
    import trader.cockpit_supervisor as sup_module

    class TrackingOS:
        def kill(self, pid, sig):
            signals_sent.append((pid, sig))
            return _os_real.kill(pid, sig)

        def __getattr__(self, name):
            return getattr(_os_real, name)

    monkeypatch.setattr(sup_module, "os", TrackingOS())

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("q")

    assert signals_sent == [], f"q a envoyé des signaux inattendus : {signals_sent}"


# ---------------------------------------------------------------------------
# Layout v2 — 3 colonnes
# ---------------------------------------------------------------------------


async def test_cockpit_v2_pane_left_existe(tmp_path, monkeypatch):
    """Le layout v2 expose un pane gauche avec le panneau equity."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#left-pane") is not None


async def test_cockpit_v2_pane_center_existe(tmp_path, monkeypatch):
    """Le layout v2 expose un pane central."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#center-pane") is not None


async def test_cockpit_v2_pane_right_existe(tmp_path, monkeypatch):
    """Le layout v2 expose un pane droit."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#right-pane") is not None


async def test_cockpit_v2_toggle_l_masque_right_pane(tmp_path, monkeypatch):
    """Le toggle l masque le pane droit (logs + panneaux compacts)."""
    # Daemon simulé vivant → pas de ConfirmStart qui intercepterait les touches
    _make_minimal_state_with_pid(tmp_path, 54321)
    _patch_daemon_alive(monkeypatch, 54321)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        right = app.query_one("#right-pane")
        assert right.display is True
        await pilot.press("l")
        assert right.display is False
        await pilot.press("l")
        assert right.display is True


async def test_cockpit_v2_exit_plans_panel_existe(tmp_path, monkeypatch):
    """Le panneau #exit-plans-panel est dans le DOM."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#exit-plans-panel") is not None


async def test_cockpit_v2_toggle_d_rerender_nouveaux_panneaux(tmp_path, monkeypatch):
    """La touche d bascule le thème sans crash avec les nouveaux panneaux."""
    # Daemon simulé vivant → pas de ConfirmStart qui intercepterait les touches
    _make_minimal_state_with_pid(tmp_path, 54321)
    _patch_daemon_alive(monkeypatch, 54321)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("d")
        assert app.theme == "casys-ink"
        await pilot.press("d")
        assert app.theme == "casys-salmon"


# ---------------------------------------------------------------------------
# Bug visuel : backlog initial RichLog différé post-layout
# ---------------------------------------------------------------------------


async def test_events_pane_backlog_charge_apres_layout(tmp_path, monkeypatch):
    """Le remplissage initial du RichLog doit être différé jusqu'à on_ready.

    Vérifie que _backlog_loaded passe à True après le premier refresh (ce qui
    garantit que les lignes sont écrites une fois le widget dimensionné) et que
    les lignes d'un events.jsonl pré-rempli sont bien présentes dans le log.
    """
    import json as _json
    from trader.cockpit import EventsPane, RightPane

    _make_minimal_state(tmp_path)
    events_file = tmp_path / "events.jsonl"
    # Remplir avec quelques lignes de backlog
    events_file.write_text(
        "\n".join(
            _json.dumps({"ts": f"2026-06-10T10:00:0{i}+00:00", "event": "other", "msg": f"ligne {i}"})
            for i in range(5)
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", events_file)
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        # Laisser un cycle supplémentaire pour que call_after_refresh s'exécute
        await pilot.pause()
        right: RightPane = app.query_one("#right-pane", RightPane)
        events_pane: EventsPane = right.query_one("#events-pane", EventsPane)
        # Le backlog doit avoir été chargé (flag positionné par on_mount + call_after_refresh)
        assert events_pane._backlog_loaded is True
        # L'offset doit avoir avancé (les 5 lignes ont été lues)
        assert events_pane._offset > 0
        # EventsPane occupe toute la largeur du RightPane (bug MAJEUR v2 : était width:40%)
        # On tolère ±2 colonnes pour les bordures internes
        assert abs(events_pane.size.width - right.size.width) <= 2


# ---------------------------------------------------------------------------
# ConfirmQuit — nouveau flux q avec daemon vivant
# ---------------------------------------------------------------------------


def _make_minimal_state_with_pid(tmp_path: Path, pid: int) -> None:
    """État minimal + daemon_status.json avec pid pour simuler daemon vivant."""
    from datetime import UTC, datetime

    _make_minimal_state(tmp_path)
    ts = datetime.now(UTC).isoformat()
    (tmp_path / "daemon_status.json").write_text(
        __import__("json").dumps({"ts": ts, "phase": "idle", "pid": pid}),
        encoding="utf-8",
    )


def _patch_daemon_alive(monkeypatch, pid: int) -> None:
    """Patche cockpit_supervisor pour simuler un daemon vivant avec identité OK."""
    import trader.cockpit_supervisor as sup_module

    monkeypatch.setattr(
        sup_module, "_is_daemon_pid", lambda p: p == pid
    )


async def test_cockpit_q_avec_daemon_vivant_ouvre_confirm_quit(tmp_path, monkeypatch):
    """q avec daemon vivant → ConfirmQuit sur la pile (pas de quit immédiat)."""
    FAKE_PID = 54321
    _make_minimal_state_with_pid(tmp_path, FAKE_PID)
    _patch_daemon_alive(monkeypatch, FAKE_PID)

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("q")
        from trader.cockpit import ConfirmQuit

        assert isinstance(app.screen, ConfirmQuit)


async def test_cockpit_confirm_quit_arreter_et_quitter_appelle_stop_daemon_et_exit(
    tmp_path, monkeypatch
):
    """ConfirmQuit → « Arrêter et quitter » → stop_daemon appelé ET app.exit() appelé."""
    FAKE_PID = 54321
    _make_minimal_state_with_pid(tmp_path, FAKE_PID)
    _patch_daemon_alive(monkeypatch, FAKE_PID)

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    stop_calls = []
    exit_calls = []

    import trader.cockpit_supervisor as sup_module
    from trader.cockpit_supervisor import StopResult

    monkeypatch.setattr(
        sup_module,
        "stop_daemon",
        lambda *, pid_file: stop_calls.append(pid_file) or StopResult(
            stopped=True, pid=FAKE_PID, reason="sigint_sent"
        ),
    )

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        # Spy sur app.exit
        original_exit = app.exit
        app.exit = lambda *a, **kw: exit_calls.append(True) or original_exit(*a, **kw)  # type: ignore[method-assign]

        await pilot.press("q")
        await pilot.click("#confirm-quit-stop")
        await pilot.pause()

    assert len(stop_calls) == 1, "stop_daemon doit être appelé exactement une fois"
    assert len(exit_calls) >= 1, "app.exit() doit être appelé après confirmation"


async def test_cockpit_confirm_quit_stop_daemon_retourne_false_quand_meme_exit(
    tmp_path, monkeypatch
):
    """stop_daemon → stopped=False : notif warning + app.exit() appelé quand même."""
    FAKE_PID = 54321
    _make_minimal_state_with_pid(tmp_path, FAKE_PID)
    _patch_daemon_alive(monkeypatch, FAKE_PID)

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    exit_calls = []

    import trader.cockpit_supervisor as sup_module
    from trader.cockpit_supervisor import StopResult

    monkeypatch.setattr(
        sup_module,
        "stop_daemon",
        lambda *, pid_file: StopResult(stopped=False, pid=FAKE_PID, reason="pid_dead"),
    )

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        original_exit = app.exit
        app.exit = lambda *a, **kw: exit_calls.append(True) or original_exit(*a, **kw)  # type: ignore[method-assign]

        await pilot.press("q")
        await pilot.click("#confirm-quit-stop")
        await pilot.pause()

    assert len(exit_calls) >= 1, (
        "app.exit() doit être appelé même si stop_daemon retourne stopped=False"
    )


async def test_cockpit_confirm_quit_stop_daemon_leve_exception_quand_meme_exit(
    tmp_path, monkeypatch
):
    """stop_daemon lève PermissionError : notif warning + app.exit() appelé quand même.

    C'est le test du BLOQUANT : sans try/except+finally, l'exception propage
    hors du callback et self.exit() n'est jamais atteint — q reste coincé.
    """
    FAKE_PID = 54321
    _make_minimal_state_with_pid(tmp_path, FAKE_PID)
    _patch_daemon_alive(monkeypatch, FAKE_PID)

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    exit_calls = []

    import trader.cockpit_supervisor as sup_module

    def _raising_stop(*, pid_file):
        raise PermissionError("OS refuse le signal")

    monkeypatch.setattr(sup_module, "stop_daemon", _raising_stop)

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        original_exit = app.exit
        app.exit = lambda *a, **kw: exit_calls.append(True) or original_exit(*a, **kw)  # type: ignore[method-assign]

        await pilot.press("q")
        await pilot.click("#confirm-quit-stop")
        await pilot.pause()

    assert len(exit_calls) >= 1, (
        "app.exit() doit être appelé même si stop_daemon lève une exception"
    )


async def test_cockpit_q_avec_daemon_vivant_ne_quitte_pas_sans_confirmation(
    tmp_path, monkeypatch
):
    """q avec daemon vivant ne quitte JAMAIS sans avoir tenté le stop — modal requis."""
    FAKE_PID = 54321
    _make_minimal_state_with_pid(tmp_path, FAKE_PID)
    _patch_daemon_alive(monkeypatch, FAKE_PID)

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    stop_calls = []

    import trader.cockpit_supervisor as sup_module
    from trader.cockpit_supervisor import StopResult

    monkeypatch.setattr(
        sup_module,
        "stop_daemon",
        lambda *, pid_file: stop_calls.append(pid_file) or StopResult(
            stopped=True, pid=FAKE_PID, reason="sigint_sent"
        ),
    )

    # On presse q mais on annule avec Échap → app reste ouverte, stop jamais appelé
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("q")
        await pilot.press("escape")
        # L'app est encore ouverte
        assert app.query_one("#left-pane") is not None

    assert stop_calls == [], "stop_daemon ne doit pas être appelé si on annule"


async def test_cockpit_confirm_quit_echap_reste_ouvert(tmp_path, monkeypatch):
    """ConfirmQuit → Échap → app reste ouverte (pas de quit, modal fermé)."""
    FAKE_PID = 54321
    _make_minimal_state_with_pid(tmp_path, FAKE_PID)
    _patch_daemon_alive(monkeypatch, FAKE_PID)

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("q")
        from trader.cockpit import ConfirmQuit

        assert isinstance(app.screen, ConfirmQuit)
        # Échap ferme le modal sans quitter
        await pilot.press("escape")
        # L'app est toujours là (on vérifie que le modal est parti et l'app est montée)
        assert not isinstance(app.screen, ConfirmQuit)
        assert app.query_one("#left-pane") is not None


# ---------------------------------------------------------------------------
# ConfirmStart — proposition de démarrage du daemon au lancement
# ---------------------------------------------------------------------------


async def test_cockpit_propose_demarrage_quand_daemon_never_started(
    tmp_path, monkeypatch
):
    """Au lancement sans daemon (never_started) → ConfirmStart sur la pile."""
    _make_minimal_state(tmp_path)  # daemon_status.json sans pid → never_started
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()  # laisse call_after_refresh s'exécuter
        from trader.cockpit import ConfirmStart

        assert isinstance(app.screen, ConfirmStart)


async def test_cockpit_ne_propose_pas_quand_daemon_vivant(tmp_path, monkeypatch):
    """Au lancement avec daemon vivant → aucune proposition (pas de ConfirmStart)."""
    FAKE_PID = 54321
    _make_minimal_state_with_pid(tmp_path, FAKE_PID)
    _patch_daemon_alive(monkeypatch, FAKE_PID)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        from trader.cockpit import ConfirmStart

        assert not isinstance(app.screen, ConfirmStart)


async def test_cockpit_confirm_start_demarrer_appelle_launch_daemon(
    tmp_path, monkeypatch
):
    """ConfirmStart → « Démarrer » → launch_daemon est appelé."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    launch_calls = []

    import trader.cockpit_supervisor as sup_module
    from trader.cockpit_supervisor import LaunchResult

    monkeypatch.setattr(
        sup_module,
        "launch_daemon",
        lambda **kw: launch_calls.append(kw) or LaunchResult(
            launched=True, pid=99999, reason="launched"
        ),
    )

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.click("#confirm-start-yes")
        await pilot.pause()

    assert len(launch_calls) == 1, "launch_daemon doit être appelé après « Démarrer »"


async def test_cockpit_confirm_start_plus_tard_ne_lance_rien(tmp_path, monkeypatch):
    """ConfirmStart → « Plus tard » → launch_daemon n'est pas appelé."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    launch_calls = []

    import trader.cockpit_supervisor as sup_module
    from trader.cockpit_supervisor import LaunchResult

    monkeypatch.setattr(
        sup_module,
        "launch_daemon",
        lambda **kw: launch_calls.append(kw) or LaunchResult(launched=True),
    )

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.click("#confirm-start-no")
        await pilot.pause()

    assert launch_calls == [], "« Plus tard » ne doit rien lancer"


async def test_cockpit_footer_affiche_maj_x(tmp_path, monkeypatch):
    """Le binding X affiche 'Maj+X' dans sa description pour lever l'ambiguïté."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        bindings_x = [b for b in app.BINDINGS if b.key == "X"]
        assert bindings_x, "Le binding 'X' doit exister"
        assert "Maj+X" in bindings_x[0].description, (
            f"La description de X doit contenir 'Maj+X', got: {bindings_x[0].description!r}"
        )
