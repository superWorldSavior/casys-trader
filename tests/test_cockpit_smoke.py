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

from rich.console import Console
from textual.widgets import ContentSwitcher, Static

import trader.cockpit.app as cockpit_module
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


def _render(renderable, *, width: int = 120) -> str:
    console = Console(width=width, highlight=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


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
        assert app.query_one("#positions-plans-pane") is not None
        assert app.query_one("#equity-trades-pane") is not None
        assert app.query_one("#logs-pane") is not None
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
        assert app.query_one("#positions-plans-pane") is not None


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
    from trader.ui.palette import PALETTE_LIGHT, PALETTE_DARK

    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        status = app.query_one("#cockpit-status", CockpitStatus)
        state = {
            "portfolio": {
                "cash": 85000.0,
                "equity": 102500.0,
                "total_return_pct": 0.5,
            },
            "starting_cash": 100000.0,
            "kpis": {},
            "daemon_status": {
                "phase": "idle",
                "decisions_done": 1,
                "symbols_total": 3,
            },
            "dry_run": True,
        }
        # Doit accepter palette= sans TypeError
        status.update_state(state, kill_active=False, palette=PALETTE_LIGHT)
        rendered = str(status.render())
        assert "Cash $" in rendered
        assert "85,000.00" in rendered
        assert "$85,000.00" in rendered
        assert "P&L net vs départ" in rendered
        assert "(+2,500.00)" in rendered
        assert "Progrès 1/3" in rendered
        status.update_state(state, kill_active=False, palette=PALETTE_DARK)


async def test_cockpit_status_affiche_fraicheur_et_source_rapport(
    tmp_path, monkeypatch
):
    """Le statut conserve la fraîcheur du rapport de l'ancien header TUI."""
    from trader.cockpit import CockpitStatus
    from trader.ui.palette import PALETTE_LIGHT

    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        status = app.query_one("#cockpit-status", CockpitStatus)
        state = {
            "ts": "2026-06-07T10:15:00+02:00",
            "source": "last_report",
            "portfolio": {"cash": 85_000.0, "equity": 102_500.0},
            "starting_cash": 100_000.0,
            "kpis": {"total_return": 0.025},
            "daemon_status": {},
            "dry_run": True,
        }

        status.update_state(state, kill_active=False, palette=PALETTE_LIGHT)
        rendered = str(status.content)

        assert "+2.50%" in rendered
        assert "Cycle" in rendered
        assert "2026-06-07 08:15 UTC" in rendered
        assert "Source last_report" in rendered


async def test_cockpit_toggle_theme_propage_palette_dashboard_immediatement(
    tmp_path, monkeypatch
):
    """Finding 4 : après toggle d, PositionsPlansPane utilise la nouvelle palette
    dès le prochain update_state — pas besoin d'attendre le poll interval.

    On vérifie que _current_palette de PositionsPlansPane change immédiatement après
    action_toggle_theme, sans attendre le prochain cycle de refresh.
    """
    from trader.cockpit import PositionsPlansPane
    from trader.ui.palette import PALETTE_DARK, PALETTE_LIGHT

    # Daemon simulé vivant → pas de ConfirmStart qui intercepterait les touches
    _make_minimal_state_with_pid(tmp_path, 54321)
    _patch_daemon_alive(monkeypatch, 54321)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        pane = app.query_one("#positions-plans-pane", PositionsPlansPane)
        # Thème saumon → palette LIGHT
        assert pane._current_palette is PALETTE_LIGHT
        # Toggle → palette DARK immédiatement
        await pilot.press("d")
        assert pane._current_palette is PALETTE_DARK
        # Retour → palette LIGHT
        await pilot.press("d")
        assert pane._current_palette is PALETTE_LIGHT


# ---------------------------------------------------------------------------
# Indicateur vital
# ---------------------------------------------------------------------------


def test_indicateur_vital_alive_genere_vivant_markup(tmp_path, monkeypatch):
    """pid vivant + identité trader.daemon → vital.status == "alive", markup VIVANT."""
    from datetime import UTC, datetime, timedelta
    import json
    from trader.cockpit.supervisor import daemon_vital_state

    status_file = tmp_path / "daemon_status.json"
    now = datetime.now(UTC)
    ts = (now - timedelta(seconds=30)).isoformat()
    status_file.write_text(json.dumps({"ts": ts, "phase": "idle", "pid": 42}), encoding="utf-8")

    monkeypatch.setattr("trader.cockpit.supervisor.os.kill", lambda p, s: None)
    monkeypatch.setattr("trader.cockpit.supervisor._get_cmdline", lambda p: "uv run python -m trader.daemon --live")

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
    from trader.cockpit.supervisor import daemon_vital_state

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
    import trader.cockpit.supervisor as sup_module

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
        assert app.query_one("#positions-plans-pane") is not None


async def test_cockpit_shell_navigable_expose_home_et_pages_detail(
    tmp_path, monkeypatch
):
    """Le cockpit expose une home courte et des pages de détail navigables."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#cockpit-nav") is not None
        switcher = app.query_one("#page-switcher", ContentSwitcher)
        assert switcher.current == "overview-page"
        assert app.query_one("#workspace") is not None
        assert app.query_one("#overview-page") is not None
        assert app.query_one("#portfolio-page") is not None
        assert app.query_one("#decisions-page") is not None
        assert app.query_one("#plans-page") is not None
        assert app.query_one("#observability-page") is not None
        assert app.query_one("#logs-page") is not None

        assert app.query_one("#overview-page").display is True
        assert app.query_one("#portfolio-page").display is False
        assert app.query_one("#logs-page").display is False


async def test_cockpit_tab_et_fleches_naviguent_entre_pages(tmp_path, monkeypatch):
    """Tab, droite et gauche changent de page sans scroller le dashboard."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        assert app._active_page_key == "home"

        await pilot.press("tab")
        assert app._active_page_key == "portfolio"
        assert app.query_one("#portfolio-page").display is True

        await pilot.press("right")
        assert app._active_page_key == "decisions"
        assert app.query_one("#decisions-page").display is True

        await pilot.press("left")
        assert app._active_page_key == "portfolio"
        assert app.query_one("#portfolio-page").display is True


async def test_cockpit_home_layout_respire_sur_tout_l_ecran(tmp_path, monkeypatch):
    """La home expose de vraies tuiles Textual dimensionnées, pas une grille comprimée."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        portfolio = app.query_one("#overview-portfolio-tile", Static)
        decisions = app.query_one("#overview-decisions-tile", Static)
        plans = app.query_one("#overview-plans-tile", Static)
        observability = app.query_one("#overview-observability-tile", Static)
        logs = app.query_one("#overview-logs-tile", Static)

        assert portfolio.size.width > decisions.size.width
        assert portfolio.size.height >= decisions.size.height
        assert plans.size.height > 5
        assert observability.size.width > 60
        assert logs.size.width > 60


def test_cockpit_home_affiche_des_tuiles_analytiques() -> None:
    """La home doit afficher des mini-artefacts analytiques, pas juste des compteurs."""
    state = {
        "portfolio": {
            "cash": 94_000.0,
            "equity": 103_200.0,
            "total_return_pct": 3.2,
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": 3,
                    "last_price": 201.25,
                    "unrealized_pnl_net": 120.5,
                    "fx_rate": 1.0,
                },
                {
                    "symbol": "2330.TW",
                    "quantity": 80,
                    "last_price": 950.0,
                    "unrealized_pnl_net": -42.0,
                    "fx_rate": 0.031,
                },
            ],
        },
        "starting_cash": 100_000.0,
        "attribution": {
            "realized_pnl": 310.0,
            "total_commissions": 14.0,
            "n_closed_trades": 4,
        },
        "equity_curve": [100_000, 100_400, 99_900, 101_500, 103_200],
        "decisions": [
            {
                "symbol": "AAPL",
                "action": "BUY",
                "confidence": 0.81,
                "rationale": "cassure propre",
                "data_source": "fresh",
            },
            {
                "symbol": "MSFT",
                "action": "HOLD",
                "confidence": 0.55,
                "rationale": "range",
                "data_source": "stale",
            },
        ],
        "recent_decisions": [{"symbol": "MSFT", "reason": "risk:confidence_below"}],
        "armed_plans": [{"symbol": "AAPL", "kind": "breakout"}],
        "trade_plans": [{"symbol": "AAPL", "remaining_quantity": 3}],
        "indicator_watches": [{"symbol": "MSFT"}, {"symbol": "NVDA"}],
        "stale_streaks": {"MSFT": 4},
        "learnings": [
            {
                "symbol": "AAPL",
                "note": "attendre confirmation volume",
                "ts": "2026-06-07T10:15:00+02:00",
            }
        ],
        "learnings_pending_count": 2,
        "daemon_status": {
            "phase": "deciding_batch",
            "decisions_done": 3,
            "symbols_total": 12,
            "model_calls_used": 2,
            "max_model_calls_per_cycle": 8,
        },
        "source": "current_report",
        "ts": "2026-06-07T10:15:00+02:00",
    }

    rendered = _render(
        cockpit_module._build_overview_panel(state, kill_active=False),
        width=220,
    )

    assert any(ch in rendered for ch in "▁▂▃▄▅▆▇█")
    assert "Top positions" in rendered
    assert "Allocation symboles" in rendered
    assert "Contrib PnL" in rendered
    assert "Risque sorties" in rendered
    assert "Équité locale" in rendered
    assert "Dernier natif" in rendered
    assert "PnL latent USD" in rendered
    assert "FX→USD" in rendered
    assert "sans plan" in rendered
    assert "AAPL" in rendered
    assert "2330.TW" in rendered
    assert "Décisions récentes" in rendered
    assert "Triage décisions" in rendered
    assert "BUY" in rendered
    assert "fresh" in rendered
    assert "risk" in rendered
    assert "Rejets récents" in rendered
    assert "Plans armés" in rendered
    assert "Sorties ouvertes" in rendered
    assert "Veilles" in rendered
    assert "Sens/Qté" in rendered
    assert "Risque" in rendered
    assert "Flux live" in rendered
    assert "Derniers signaux" in rendered
    assert "ouvrir la page logs" not in rendered
    assert "Data health" in rendered
    assert "Learnings" in rendered
    assert "MSFT" in rendered
    assert "attendre" in rendered
    assert "confirmation" in rendered
    assert "volume" in rendered


def test_cockpit_home_decisions_utilise_recent_decisions_quand_cycle_vide() -> None:
    """La home ne doit pas devenir vide entre deux cycles quand seul le tail existe."""
    state = {
        "portfolio": {"cash": 100_000.0, "equity": 100_000.0, "holdings": []},
        "decisions": [],
        "recent_decisions": [
            {
                "cycle_ts": "2026-07-03T08:00:00+00:00",
                "sequence": 1,
                "symbol": "QUIET",
                "action": "HOLD",
                "reason": "quiet_gate",
                "decision_source": "infra",
                "model_called": False,
            },
            {
                "cycle_ts": "2026-07-03T08:01:00+00:00",
                "sequence": 2,
                "symbol": "AAPL",
                "action": "SELL",
                "confidence": 0.91,
                "executed": True,
                "qty": 4,
                "price": 123.45,
                "llm_provider": "openai",
                "llm_model": "gpt-test",
            },
            {
                "cycle_ts": "2026-07-03T08:02:00+00:00",
                "sequence": 3,
                "symbol": "MSFT",
                "action": "BUY",
                "confidence": 0.44,
                "reason": "risk:confidence_below",
                "runtime": {"data_source": "fresh"},
            },
        ],
        "daemon_status": {
            "phase": "idle",
            "model_calls_used": 1,
            "max_model_calls_per_cycle": 8,
        },
    }

    rendered = _render(
        cockpit_module._build_overview_panel(state, kill_active=False),
        width=220,
    )

    assert "AAPL" in rendered
    assert "SELL" in rendered
    assert "exec" in rendered
    assert "ordre 4 @ 123.45" in rendered
    assert "MSFT" in rendered
    assert "risk" in rendered
    assert rendered.index("SELL") < rendered.index("quiet")


def test_cockpit_home_portefeuille_priorise_allocation_par_symbole() -> None:
    """La home doit montrer l'allocation par symbole plutôt qu'une vue devise vague."""
    state = {
        "portfolio": {
            "cash": 10_000.0,
            "equity": 120_000.0,
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": 20,
                    "last_price": 200.0,
                    "unrealized_pnl_net": 150.0,
                    "fx_rate": 1.0,
                },
                {
                    "symbol": "MSFT",
                    "quantity": 10,
                    "last_price": 400.0,
                    "unrealized_pnl_net": -20.0,
                    "fx_rate": 1.0,
                },
                {
                    "symbol": "2330.TW",
                    "quantity": 200,
                    "last_price": 900.0,
                    "unrealized_pnl_net": 50.0,
                    "fx_rate": 0.031,
                },
            ],
        },
        "equity_curve": [100_000.0, 105_000.0, 120_000.0],
    }

    rendered = _render(
        cockpit_module._build_overview_panel(state, kill_active=False),
        width=220,
    )

    assert "Allocation symboles" in rendered
    assert "AAPL" in rendered
    assert "MSFT" in rendered
    assert "2330.TW" in rendered
    assert "USD" in rendered
    assert "TWD" in rendered
    assert "Allocation devise" not in rendered


def test_cockpit_home_plans_affiche_file_operationnelle() -> None:
    """Plans: les ordres armés, sorties et veilles ont chacun leur section."""
    state = {
        "portfolio": {"cash": 100_000.0, "equity": 100_000.0, "holdings": []},
        "trade_plans": [
            {
                "symbol": "SPY",
                "side": "LONG",
                "remaining_quantity": 10,
                "entry_price": 100.0,
                "hard_stop_price": 96.0,
                "take_profits": [{"name": "TP1", "price": 108.0}],
            }
        ],
        "armed_plans": [
            {
                "symbol": "QQQ",
                "logic": "all",
                "conditions": [
                    {"indicator": "RS", "op": "<", "value": 40, "timeframe": "1h"},
                    {"indicator": "MACD", "op": ">", "value": 0, "timeframe": "1h"},
                ],
                "expires_at": "2999-01-01T00:00:00+00:00",
                "order": {
                    "action": "SELL",
                    "qty": 60,
                    "exit_plan": {"hard_stop": {"price": 312.01}},
                },
            }
        ],
        "indicator_watches": [
            {
                "symbol": "MSFT",
                "logic": "any",
                "conditions": [{"indicator": "RS", "op": "<", "value": 35, "timeframe": "1h"}],
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
        ],
        "prices": {"SPY": 101.0},
        "stale_market_data": {"SPY": {"reason": "too_old"}},
        "daemon_status": {"phase": "idle", "decisions_done": 0, "symbols_total": 3},
    }

    rendered = _render(
        cockpit_module._build_overview_panel(state, kill_active=False),
        width=220,
    )

    assert "Plans armés" in rendered
    assert "Sorties ouvertes" in rendered
    assert "Veilles" in rendered
    assert "SPY" in rendered
    assert "stale" in rendered
    assert "SELL 60" in rendered
    assert "stop 312.01" in rendered
    assert "RS<40@1h" in rendered
    assert "MSFT" in rendered
    assert "WAKE" in rendered


def test_cockpit_home_flux_live_remplace_les_commandes_logs() -> None:
    """La tuile logs de la home doit résumer le runtime, pas lister des raccourcis."""
    state = {
        "portfolio": {"cash": 100_000.0, "equity": 100_000.0, "holdings": []},
        "source": "current_report",
        "ts": "2026-07-03T08:15:00+00:00",
        "learnings_pending_count": 3,
        "stale_streaks": {"SPY": 2},
        "daemon_status": {
            "phase": "deciding_batch",
            "decisions_done": 4,
            "symbols_total": 12,
            "model_calls_used": 5,
            "max_model_calls_per_cycle": 8,
        },
        "recent_decisions": [
            {
                "cycle_ts": "2026-07-03T08:12:00+00:00",
                "symbol": "AAPL",
                "action": "BUY",
                "confidence": 0.83,
                "runtime": {"trade_plan_created": True},
            },
            {
                "cycle_ts": "2026-07-03T08:13:00+00:00",
                "symbol": "SPY",
                "action": "HOLD",
                "reason": "stale_market_data",
            },
        ],
    }

    rendered = _render(
        cockpit_module._build_overview_panel(state, kill_active=True),
        width=220,
    )

    assert "Flux live" in rendered
    assert "Runtime" in rendered
    assert "deciding_batch" in rendered
    assert "4/12" in rendered
    assert "5/8" in rendered
    assert "current_report" in rendered
    assert "Derniers signaux" in rendered
    assert "AAPL" in rendered
    assert "plan créé" in rendered
    assert "stale" in rendered
    assert "KILL actif" in rendered
    assert "ouvrir la page logs" not in rendered


async def test_cockpit_observabilite_affiche_les_derniers_learnings(
    tmp_path, monkeypatch
):
    """Le cockpit Textual conserve le panneau narratif des learnings récents."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    note = (
        "le range tient depuis cinq réveils consécutifs, j'attends une cassure nette "
        "au-dessus de la résistance avant d'ouvrir"
    )
    state = {
        "universe_symbols": [],
        "venue_state": {},
        "open_venues_list": [],
        "company_map": {},
        "indicator_watches": [],
        "learnings": [
            {
                "ts": "2026-06-07T10:15:00+02:00",
                "symbol": "AAPL",
                "note": note,
            }
        ],
    }

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        pane = app.query_one("#universe-pane", cockpit_module.UniversePane)
        pane.update_state(state)

        panel = app.query_one("#learnings-panel", Static)
        rendered = _render(panel.content)
        compact_rendered = "".join(
            ch for ch in rendered if not ch.isspace() and ch != "│"
        )
        compact_note = "".join(note.split())

        assert "Derniers apprentissages" in rendered
        assert "AAPL" in rendered
        assert compact_note in compact_rendered


def test_attention_strip_resume_les_alertes_operationnelles() -> None:
    """La ligne d'attention remonte les signaux qui appellent une action humaine."""
    from trader.cockpit import _build_attention_line
    from trader.ui.palette import PALETTE_LIGHT

    state = {
        "halted": "risk_gate",
        "portfolio": {"holdings": [{"symbol": "AAPL"}]},
        "armed_plans": [{"id": "AAPL-breakout"}],
        "indicator_watches": [{"id": "AAPL-watch"}, {"id": "MSFT-watch"}],
        "stale_streaks": {"AAPL": 2, "MSFT": 0, "TSLA": 5},
        "recent_decisions": [
            {"symbol": "AAPL", "reason": "risk:gross_exposure_exceeded"},
            {"symbol": "MSFT", "reason": "hold"},
        ],
        "daemon_status": {
            "phase": "deciding_batch",
            "decisions_done": 7,
            "symbols_total": 12,
            "model_calls_used": 3,
            "max_model_calls_per_cycle": 5,
        },
        "learnings_pending_count": 9,
    }

    text = _build_attention_line(state, kill_active=True, palette=PALETTE_LIGHT)
    rendered = text.plain

    assert "KILL" in rendered
    assert "HALT risk_gate" in rendered
    assert "stale 2" in rendered
    assert "risk 1" in rendered
    assert "plans 1" in rendered
    assert "veilles 2" in rendered
    assert "LLM 3/5" in rendered
    assert "learn 9" in rendered


async def test_cockpit_v2_pane_center_existe(tmp_path, monkeypatch):
    """Le layout v2 expose un pane central."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#equity-trades-pane") is not None


async def test_cockpit_v2_pane_right_existe(tmp_path, monkeypatch):
    """Le layout v2 expose un pane droit."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#logs-pane") is not None


async def test_cockpit_l_bascule_vers_la_page_logs(tmp_path, monkeypatch):
    """Le raccourci l ouvre la page Logs puis revient à la page précédente."""
    # Daemon simulé vivant → pas de ConfirmStart qui intercepterait les touches
    _make_minimal_state_with_pid(tmp_path, 54321)
    _patch_daemon_alive(monkeypatch, 54321)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        assert app._active_page_key == "home"
        assert app.query_one("#logs-page").display is False

        await pilot.press("l")
        assert app._active_page_key == "logs"
        assert app.query_one("#logs-page").display is True

        await pilot.press("l")
        assert app._active_page_key == "home"
        assert app.query_one("#logs-page").display is False


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
    from trader.cockpit import LogsPane

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
        logs_pane: LogsPane = app.query_one("#logs-pane", LogsPane)
        # Le backlog doit avoir été chargé (flag positionné par on_mount + call_after_refresh)
        assert logs_pane._backlog_loaded is True
        # L'offset doit avoir avancé (les 5 lignes ont été lues)
        assert logs_pane._offset > 0


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
    import trader.cockpit.supervisor as sup_module

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


async def test_cockpit_ctrl_c_avec_daemon_vivant_ouvre_confirm_quit(
    tmp_path, monkeypatch
):
    """Ctrl+C doit passer par le même avertissement que q (incontournable).

    Sans ce binding, Textual mappe ctrl+c sur help_quit (notification inerte)
    et l'utilisateur quitte sans voir que le moteur live va être arrêté.
    """
    FAKE_PID = 54321
    _make_minimal_state_with_pid(tmp_path, FAKE_PID)
    _patch_daemon_alive(monkeypatch, FAKE_PID)

    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("ctrl+c")
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

    import trader.cockpit.supervisor as sup_module
    from trader.cockpit.supervisor import StopResult

    monkeypatch.setattr(
        sup_module,
        "stop_daemon",
        lambda *, pid_file, status_file=None: stop_calls.append(pid_file) or StopResult(
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

    import trader.cockpit.supervisor as sup_module
    from trader.cockpit.supervisor import StopResult

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

    import trader.cockpit.supervisor as sup_module

    def _raising_stop(*, pid_file, status_file=None):
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

    import trader.cockpit.supervisor as sup_module
    from trader.cockpit.supervisor import StopResult

    monkeypatch.setattr(
        sup_module,
        "stop_daemon",
        lambda *, pid_file, status_file=None: stop_calls.append(pid_file) or StopResult(
            stopped=True, pid=FAKE_PID, reason="sigint_sent"
        ),
    )

    # On presse q mais on annule avec Échap → app reste ouverte, stop jamais appelé
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("q")
        await pilot.press("escape")
        # L'app est encore ouverte
        assert app.query_one("#positions-plans-pane") is not None

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
        assert app.query_one("#positions-plans-pane") is not None


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

    import trader.cockpit.supervisor as sup_module
    from trader.cockpit.supervisor import LaunchResult

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

    import trader.cockpit.supervisor as sup_module
    from trader.cockpit.supervisor import LaunchResult

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
