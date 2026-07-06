"""Tests du socle redesign casys : shell (rail/KPI/footer), derive, format, app.

Remplace les tests du shell historique (3 barres empilées + cycle de thèmes).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.derive import (
    cycle_progress,
    equity_snapshot,
    health_alert_count,
    journal_entries,
    next_to_fire,
    rail_vitals,
)
from trader.interfaces.cockpit.first_run import FirstRunScreen, preflight_checks
from trader.interfaces.cockpit.modals import ConfirmKill, ConfirmStop, HelpOverlay
from trader.interfaces.cockpit.shell import (
    NavItem,
    build_footer,
    build_kpi_band,
    build_rail_nav,
    build_rail_vitals,
)
from trader.interfaces.cockpit.supervisor import DaemonVitalState

UTC = timezone.utc
NOW = datetime(2026, 7, 6, 2, 1, 28, tzinfo=UTC)

_NAV = (
    NavItem("home", 1, "home"),
    NavItem("health", 5, "health"),
    NavItem("settings", 8, "settings"),
)


def _render(renderable, width: int = 100) -> str:
    from rich.console import Console

    console = Console(width=width, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")


def _write_daemon_alive(tmp_path, monkeypatch, pid: int = 4242) -> None:
    (tmp_path / "daemon_status.json").write_text(
        f'{{"pid": {pid}, "ts": "{NOW.isoformat()}", "phase": "cycle_completed"}}',
        encoding="utf-8",
    )
    alive = DaemonVitalState(status="alive", since_seconds=5.0, battement_old=False)
    monkeypatch.setattr(cockpit_module, "daemon_vital_state", lambda _path: alive)


# ---------------------------------------------------------------------------
# format
# ---------------------------------------------------------------------------


def test_countdown_formats():
    base = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
    assert f.countdown("2026-07-06T03:58:30+00:00", now=base) == "3h58"
    assert f.countdown("2026-07-06T00:14:10+00:00", now=base) == "14m"
    assert f.countdown("2026-07-06T00:14:10+00:00", now=base, prefix="in ") == "in 14m"
    assert f.countdown("2026-07-05T23:00:00+00:00", now=base) == "expired"
    assert f.countdown(None, now=base) == "—"


def test_conf_meter_fills_by_confidence():
    rendered = _render(f.conf_meter(0.72))
    assert rendered.count("▮") == 10
    assert "0.72" in rendered


def test_ttl_fraction_bounds():
    watch = {
        "created_at": "2026-07-06T00:00:00+00:00",
        "expires_at": "2026-07-06T04:00:00+00:00",
    }
    mid = datetime(2026, 7, 6, 1, 0, 0, tzinfo=UTC)
    assert f.ttl_fraction(watch, now=mid) == pytest.approx(0.75)
    after = datetime(2026, 7, 6, 9, 0, 0, tzinfo=UTC)
    assert f.ttl_fraction(watch, now=after) == 0.0


def test_age_m_switches_to_hours():
    assert f.age_m(3) == "3m"
    assert f.age_m(4763.96) == "79h"


def test_decision_effect_fill_and_watch():
    fill = {"executed": True, "qty": 17.5, "price": 1230.0}
    text, kind = f.decision_effect(fill)
    assert kind == "fill"
    assert "17.5" in text and "1,230" in text

    watch = {
        "executed": False,
        "runtime": {"indicator_watch_created": True},
        "decision": {
            "indicator_watch": {
                "conditions": [{"indicator": "z_score", "op": "<=", "value": 0.9, "timeframe": "1h"}],
                "logic": "all",
            }
        },
    }
    text, kind = f.decision_effect(watch)
    assert kind == "watch"
    assert "z_score" in text


# ---------------------------------------------------------------------------
# derive
# ---------------------------------------------------------------------------


def _state_sample() -> dict:
    return {
        "portfolio": {
            "equity": 100053.0,
            "cash": 78187.0,
            "total_return_pct": 5.35,
            "holdings": [
                {"symbol": "WELL", "quantity": 15, "last_price": 235.87, "fx_rate": 1.0, "unrealized_pnl_net": 170.0},
                {"symbol": "9910.TW", "quantity": -2000, "last_price": 67.4, "fx_rate": 0.031, "unrealized_pnl_net": -75.0},
            ],
        },
        "starting_cash": 95000.0,
        "daemon_status": {"phase": "deciding", "decisions_done": 4, "symbols_total": 5, "model_calls_used": 4},
        "default_next_wake": "2026-07-06T02:15:28+00:00",
        "recent_decisions": [
            {
                "cycle_ts": "2026-07-06T02:01:00+00:00",
                "symbol": "3443.TW",
                "action": "HOLD",
                "confidence": 0.72,
                "rationale": "Existing long is tiny but fragile.",
                "runtime": {"indicator_watch_created": True},
                "decision": {
                    "indicator_watch": {
                        "conditions": [{"indicator": "chart_breakout", "op": "<=", "value": -1, "timeframe": "1h"}],
                        "logic": "all",
                        "expires_at": "2026-07-06T05:59:28+00:00",
                    }
                },
            },
        ],
        "indicator_watches": [
            {
                "symbol": "1440.TW",
                "created_at": "2026-07-06T00:00:00+00:00",
                "expires_at": "2026-07-06T04:59:28+00:00",
                "logic": "all",
                "conditions": [{"indicator": "z_score", "op": "<=", "value": 0.9, "timeframe": "1h"}],
            }
        ],
        "stale_market_data": {"AAPL": {"data_age_minutes": 4700.0}},
    }


def test_equity_snapshot_values():
    snap = equity_snapshot(_state_sample())
    assert snap.equity == 100053.0
    assert snap.return_pct == pytest.approx(5.35)
    assert snap.unrealized == pytest.approx(95.0)
    assert snap.cash_ledger == pytest.approx(78187.0)
    assert snap.cash_available == pytest.approx(74008.2, abs=0.1)
    assert snap.cash_pct == pytest.approx(73.97, abs=0.1)


def test_cycle_progress_running():
    cycle = cycle_progress(_state_sample())
    assert cycle.running is True
    assert (cycle.done, cycle.total) == (4, 5)


def test_journal_entries_prefer_rationale_and_append_expiry():
    entries = journal_entries(_state_sample(), now=NOW)
    assert entries[0].symbol == "3443.TW"
    assert entries[0].effect_kind == "watch"
    assert "expires 3h58" in entries[0].effect


def test_next_to_fire_sorted_with_wake():
    items = next_to_fire(_state_sample(), now=NOW)
    assert [item.kind for item in items[:2]] == ["wake", "watch"]
    assert items[0].countdown == "14m"


def test_health_alert_count_counts_stale_symbols():
    assert health_alert_count(_state_sample(), kill_active=False, now=NOW) >= 1


# ---------------------------------------------------------------------------
# shell builders
# ---------------------------------------------------------------------------


def test_rail_nav_marks_active_and_health_badge():
    rendered = _render(build_rail_nav(_NAV, active_key="home", health_alerts=19))
    assert "▎1 home" in rendered
    assert "▲19" in rendered


def test_rail_vitals_running_and_kill():
    vitals = rail_vitals(
        {"daemon_status": {"pid": 88692}, "dry_run": False, "sessions": {}},
        vital=DaemonVitalState(status="alive", since_seconds=2.0, battement_old=False),
        kill_active=False,
        now=NOW,
    )
    rendered = _render(build_rail_vitals(vitals))
    assert "● running" in rendered
    assert "pid 88692" in rendered
    assert "PAPER·LIVE" in rendered
    assert "kill ○ off" in rendered
    assert "02:01:28 UTC" in rendered

    killed = rail_vitals(
        {"daemon_status": {}, "dry_run": True, "sessions": {}},
        vital=DaemonVitalState(status="stopped", since_seconds=None, battement_old=False),
        kill_active=True,
        now=NOW,
    )
    rendered = _render(build_rail_vitals(killed))
    assert "!! KILL !!" in rendered
    assert "● stopped" in rendered
    assert "DRY RUN" in rendered


def test_kpi_band_contents():
    rendered = _render(build_kpi_band(_state_sample(), now=NOW), width=160)
    assert "EQUITY" in rendered
    assert "$100,053" in rendered
    assert "CASH FREE" in rendered
    assert "$74,008" in rendered
    assert "+5.35%" in rendered
    assert "4/5" in rendered
    assert "in 14m" in rendered


def test_kpi_band_next_wake_uses_next_future_symbol_wake_when_global_expired():
    state = _state_sample()
    state["default_next_wake"] = "2026-07-06T01:00:00+00:00"
    state["symbol_wakes"] = {
        "OLD.TW": "2026-07-06T01:30:00+00:00",
        "NEXT.TW": "2026-07-06T02:31:28+00:00",
        "LATER.TW": "2026-07-06T03:01:28+00:00",
    }

    rendered = _render(build_kpi_band(state, now=NOW), width=160)

    assert "in 30m" in rendered
    assert "expired" not in rendered


def test_footer_contextual_groups():
    home = _render(build_footer("home"), width=200)
    assert "1-8" in home and "scroll journal" in home and "kill-switch" in home and "help" in home
    universe = _render(build_footer("universe"), width=200)
    assert "pin" in universe and "ban" in universe and "undo override" in universe


# ---------------------------------------------------------------------------
# first run — preflight
# ---------------------------------------------------------------------------


def test_preflight_checks_fresh_state(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "risk.yaml").write_text("max_order_value: 10000\n", encoding="utf-8")
    (tmp_path / "mandate").mkdir()
    (tmp_path / "mandate" / "mandate.md").write_text("# mandate", encoding="utf-8")
    (tmp_path / "mandate" / "memory.md").write_text("# memory", encoding="utf-8")

    checks = {check.key: check for check in preflight_checks(tmp_path, state_dir=tmp_path / "state")}
    assert checks["config"].ok is True
    assert checks["mandate"].ok is True
    assert "fresh" in checks["state"].detail


# ---------------------------------------------------------------------------
# app — montage, thème, navigation, modals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_mounts_shell_with_casys_theme(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        assert app.theme == "casys"
        assert app.query_one("#nav-rail") is not None
        assert app.query_one("#kpi-band") is not None
        assert app.query_one("#cockpit-footer") is not None
        for key in ("home", "portfolio", "decisions", "health", "logs", "universe", "settings"):
            assert app.query_one(f"#{key}-page") is not None


@pytest.mark.asyncio
async def test_app_number_keys_navigate_all_pages(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        assert app._active_page_key == "home"
        for number, key in zip("2345671", ("portfolio", "decisions", "health", "logs", "universe", "settings", "home")):
            await pilot.press(number)
            assert app._active_page_key == key


@pytest.mark.asyncio
async def test_x_opens_confirm_stop_and_k_opens_confirm_kill(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("x")
        assert isinstance(app.screen, ConfirmStop)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("k")
        assert isinstance(app.screen, ConfirmKill)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_question_mark_opens_help_overlay(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpOverlay)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_first_run_screen_on_fresh_state(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, FirstRunScreen)


@pytest.mark.asyncio
async def test_no_preflight_when_daemon_alive(tmp_path, monkeypatch):
    """Daemon vivant → le cockpit monte directement, pas de preflight."""
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(app.screen, FirstRunScreen)


async def test_preflight_when_daemon_stopped_despite_history(tmp_path, monkeypatch):
    """Daemon arrêté (même avec historique) → preflight, esc passe au cockpit."""
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "daemon_status.json").write_text('{"pid": 99999}', encoding="utf-8")
    stopped = DaemonVitalState(status="stopped", since_seconds=None, battement_old=False)
    monkeypatch.setattr(cockpit_module, "daemon_vital_state", lambda _p: stopped)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, FirstRunScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, FirstRunScreen)


@pytest.mark.asyncio
async def test_theme_cycle_is_gone():
    assert not hasattr(cockpit_module, "_THEME_CYCLE")
    assert not any(b.key == "d" for b in CockpitApp.BINDINGS)


def test_app_paths_point_to_repo_root():
    """_ROOT/_STATE_DIR/_CONFIG_DIR pointent la racine du repo (invariant porté du smoke legacy)."""
    from pathlib import Path

    root = Path(cockpit_module.__file__).resolve().parents[3]
    assert Path(cockpit_module._ROOT) == root
    assert Path(cockpit_module._STATE_DIR) == root / "state"
    assert Path(cockpit_module._CONFIG_DIR) == root


async def test_ctrl_c_with_alive_daemon_opens_confirm_quit(tmp_path, monkeypatch):
    from trader.interfaces.cockpit.modals import ConfirmQuit

    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "trader.interfaces.cockpit.supervisor.daemon_vital_state",
        lambda _p: DaemonVitalState(status="alive", since_seconds=1.0, battement_old=False),
    )
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmQuit)
        await pilot.press("escape")


async def test_confirm_quit_exits_even_if_stop_daemon_raises(tmp_path, monkeypatch):
    """stop_daemon lève → app.exit() quand même (pas de cockpit zombie)."""
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "daemon_status.json").write_text('{"pid": 4242}', encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "trader.interfaces.cockpit.supervisor.daemon_vital_state",
        lambda _p: DaemonVitalState(status="alive", since_seconds=1.0, battement_old=False),
    )

    def _boom(**_kwargs):
        raise RuntimeError("stop failed")

    monkeypatch.setattr("trader.interfaces.cockpit.supervisor.stop_daemon", _boom)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        await pilot.click("#confirm-quit-stop")
        await pilot.pause()
    assert app._exit  # sorti malgré l'erreur


async def test_confirm_quit_keep_daemon_exits_without_stopping(tmp_path, monkeypatch):
    """« Quit, keep daemon » : le cockpit sort, stop_daemon jamais appelé."""
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "daemon_status.json").write_text('{"pid": 4242}', encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "trader.interfaces.cockpit.supervisor.daemon_vital_state",
        lambda _p: DaemonVitalState(status="alive", since_seconds=1.0, battement_old=False),
    )
    calls: list = []
    monkeypatch.setattr(
        "trader.interfaces.cockpit.supervisor.stop_daemon",
        lambda **kwargs: calls.append(kwargs),
    )
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        await pilot.click("#confirm-quit-only")
        await pilot.pause()
    assert app._exit
    assert calls == []  # le daemon survit


async def test_force_preflight_shows_screen_despite_history(tmp_path, monkeypatch):
    """--preflight force l'écran même avec un daemon vivant et de l'historique."""
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp(force_preflight=True)
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, FirstRunScreen)
        await pilot.press("escape")


def test_alert_banner_states():
    """Bandeau : None si vivant ; stopped/never/KILL/HALT avec priorité."""
    from trader.interfaces.cockpit.derive import RailVitals
    from trader.interfaces.cockpit.shell import build_alert_banner

    alive = RailVitals(running=True, vital_status="alive")
    assert build_alert_banner(alive) is None

    stopped = RailVitals(vital_status="stopped")
    rendered = _render(build_alert_banner(stopped), width=120)
    assert "daemon stopped" in rendered
    assert "nothing is trading or watching" in rendered

    never = RailVitals(vital_status="never_started")
    assert "never started" in _render(build_alert_banner(never), width=120)

    killed = RailVitals(vital_status="alive", kill_active=True)
    rendered = _render(build_alert_banner(killed), width=120)
    assert "KILL ENGAGED" in rendered

    halted = RailVitals(vital_status="alive", halted="risk_gate")
    assert "HALT risk_gate" in _render(build_alert_banner(halted), width=120)

    # priorité : KILL avant HALT avant stopped
    worst = RailVitals(vital_status="stopped", kill_active=True, halted="x")
    assert "KILL" in _render(build_alert_banner(worst), width=120)


async def test_alert_banner_visible_when_daemon_stopped(tmp_path, monkeypatch):
    from trader.interfaces.cockpit.shell import AlertBanner

    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    stopped = DaemonVitalState(status="stopped", since_seconds=None, battement_old=False)
    monkeypatch.setattr(cockpit_module, "daemon_vital_state", lambda _p: stopped)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("escape")  # ferme le preflight
        await pilot.pause()
        await pilot.pause()
        banner = app.query_one("#alert-banner", AlertBanner)
        # le refresh 2s a pu ne pas encore tourner — force un apply
        app._apply_state({"dry_run": True}, False)
        assert banner.has_class("visible")


async def test_alert_banner_hidden_when_alive(tmp_path, monkeypatch):
    from trader.interfaces.cockpit.shell import AlertBanner

    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        app._apply_state({"dry_run": True}, False)
        banner = app.query_one("#alert-banner", AlertBanner)
        assert not banner.has_class("visible")


async def test_adaptive_columns_drop_on_narrow_terminal(tmp_path, monkeypatch):
    """En terminal étroit, portfolio droppe AVG/VALUE et decisions droppe SOURCE."""
    from trader.interfaces.cockpit.pages.decisions import DecisionsPage
    from trader.interfaces.cockpit.pages.portfolio import PortfolioPage

    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(70, 26)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        page = app.query_one("#portfolio-page", PortfolioPage)
        page.update_state({"portfolio": {"holdings": [
            {"symbol": "WELL", "quantity": 15, "last_price": 235.87, "avg_price": 224.0,
             "fx_rate": 1.0, "unrealized_pnl_net": 170.0},
        ]}})
        await pilot.pause()
        assert page._active_drops  # au moins AVG droppée
        assert "AVG" in page._active_drops

        await pilot.press("3")
        await pilot.pause()
        dec = app.query_one("#decisions-page", DecisionsPage)
        dec.update_state({"recent_decisions": [
            {"cycle_ts": "2026-07-06T02:00:00+00:00", "symbol": "WELL", "action": "HOLD",
             "confidence": 0.7, "reason": "hold", "decision_source": "llm", "model_called": True},
        ]})
        await pilot.pause()
        assert dec._drop_source is True


async def test_adaptive_columns_full_on_wide_terminal(tmp_path, monkeypatch):
    from trader.interfaces.cockpit.pages.portfolio import PortfolioPage

    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        page = app.query_one("#portfolio-page", PortfolioPage)
        page.update_state({"portfolio": {"holdings": []}})
        await pilot.pause()
        assert page._active_drops == frozenset()


async def test_cursor_survives_refresh(tmp_path, monkeypatch):
    """Le curseur ne saute plus en tête quand le refresh 2 s repopule la table."""
    from trader.interfaces.cockpit.pages.portfolio import PortfolioPage

    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    _write_daemon_alive(tmp_path, monkeypatch)
    state = {"portfolio": {"holdings": [
        {"symbol": s, "quantity": 10, "last_price": 100.0, "avg_price": 99.0,
         "fx_rate": 1.0, "unrealized_pnl_net": float(i)}
        for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"])
    ]}}
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        app._last_state = state
        await pilot.press("2")
        await pilot.pause()
        page = app.query_one("#portfolio-page", PortfolioPage)
        page.update_state(state)
        await pilot.pause()
        table = page.query_one("#positions-table")
        table.focus()
        await pilot.press("down", "down")  # curseur sur la 3e ligne
        before = table.cursor_row
        assert before == 2
        page.update_state(state)  # simule le refresh périodique
        await pilot.pause()
        assert table.cursor_row == before
