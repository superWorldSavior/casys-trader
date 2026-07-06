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
    assert snap.cash_pct == pytest.approx(78.14, abs=0.1)


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
    assert "+5.35%" in rendered
    assert "4/5" in rendered
    assert "in 14m" in rendered


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
        for key in ("home", "portfolio", "decisions", "plans", "health", "logs", "universe", "settings"):
            assert app.query_one(f"#{key}-page") is not None


@pytest.mark.asyncio
async def test_app_number_keys_navigate_all_pages(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    _write_daemon_alive(tmp_path, monkeypatch)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        assert app._active_page_key == "home"
        for number, key in zip("23456781", ("portfolio", "decisions", "plans", "health", "logs", "universe", "settings", "home")):
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
async def test_no_first_run_when_history_exists(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(app.screen, FirstRunScreen)


@pytest.mark.asyncio
async def test_theme_cycle_is_gone():
    assert not hasattr(cockpit_module, "_THEME_CYCLE")
    assert not any(b.key == "d" for b in CockpitApp.BINDINGS)
