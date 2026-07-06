"""Tests de la page Logs (pages/logs.py) : backlog, filtres, chips, supervision.

Couvre aussi les flux supervision de l'app (q avec daemon vivant, s start).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.events import EventClass
from trader.interfaces.cockpit.modals import ConfirmQuit
from trader.interfaces.cockpit.pages.logs import LogsPane, build_filter_chips
from trader.interfaces.cockpit.supervisor import DaemonVitalState

UTC = timezone.utc
NOW = datetime(2026, 7, 6, 2, 0, 0, tzinfo=UTC)


def _render(renderable, width: int = 160) -> str:
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


def _write_events(tmp_path, events: list[dict]) -> None:
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


_EVENTS = [
    {"ts": "2026-07-06T01:00:00+00:00", "event": "cycle_started", "symbols_due": ["A"]},
    {
        "ts": "2026-07-06T01:01:00+00:00",
        "event": "decision_recorded",
        "symbol": "WELL",
        "action": "BUY",
        "executed": True,
        "reason": "ok",
    },
    {
        "ts": "2026-07-06T01:02:00+00:00",
        "event": "decision_recorded",
        "symbol": "1326.TW",
        "action": "HOLD",
        "executed": False,
        "reason": "hold",
    },
]


# ---------------------------------------------------------------------------
# chips
# ---------------------------------------------------------------------------


def test_filter_chips_reflect_state():
    rendered = _render(
        build_filter_chips(class_filter=None, show_cycles=False, regex_text=None, follow=True)
    )
    assert "fills ✓" in rendered
    assert "cycles ✗" in rendered  # masqués par défaut
    assert "regex —" in rendered
    assert "follow ✓" in rendered

    filtered = _render(
        build_filter_chips(
            class_filter={EventClass.DECISION_EXECUTED},
            show_cycles=False,
            regex_text="2303",
            follow=False,
        )
    )
    assert "fills ✓" in filtered
    assert "holds ✗" in filtered
    assert "regex 2303" in filtered
    assert "follow ✗" in filtered


# ---------------------------------------------------------------------------
# LogsPane — backlog + filtres
# ---------------------------------------------------------------------------


async def test_logs_backlog_loads_and_cycles_hidden_by_default(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")  # pas de first-run
    _write_events(tmp_path, _EVENTS)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("6")
        await pilot.pause()
        pane = app.query_one("#events-panel", LogsPane)
        assert pane._backlog_loaded is True
        assert pane._offset > 0
        assert len(pane._buffer) == 3
        # cycles masqués par défaut (design) — le buffer les garde, l'affichage non
        assert pane._show_cycles is False


async def test_logs_class_and_regex_filters(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    _write_events(tmp_path, _EVENTS)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("6")
        await pilot.pause()
        pane = app.query_one("#events-panel", LogsPane)
        pane.set_filters({EventClass.DECISION_EXECUTED}, None)
        assert [ln for ln in pane._buffer if pane._passes(ln)][0].markup_class is EventClass.DECISION_EXECUTED
        pane.set_filters(None, "1326")
        passing = [ln for ln in pane._buffer if pane._passes(ln)]
        assert len(passing) == 1
        assert "1326.TW" in passing[0].text
        # regex invalide → ignorée, pas d'exception
        pane.set_filters(None, "([")
        assert pane._regex is None


# ---------------------------------------------------------------------------
# supervision : q avec daemon vivant, s start
# ---------------------------------------------------------------------------


async def test_q_with_alive_daemon_opens_confirm_quit(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "daemon_status.json").write_text(
        json.dumps({"pid": 4242, "ts": NOW.isoformat(), "phase": "cycle_completed"}),
        encoding="utf-8",
    )
    alive = DaemonVitalState(status="alive", since_seconds=1.0, battement_old=False)
    monkeypatch.setattr(cockpit_module, "daemon_vital_state", lambda _p: alive)
    monkeypatch.setattr(
        "trader.interfaces.cockpit.supervisor.daemon_vital_state", lambda _p: alive
    )
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmQuit)
        await pilot.press("escape")


async def test_q_without_daemon_exits_directly(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert app.return_value is None  # sortie propre, pas de modal


def test_read_new_text_lines_gros_fichier_lit_la_queue(tmp_path):
    """_read_new_text_lines sur un fichier > 1 MiB retourne la queue, pas le début."""
    from trader.interfaces.cockpit.pages.logs import _read_new_text_lines

    f = tmp_path / "agent_trace.log"
    # ~72 octets/ligne × 16 000 ≈ 1.15 MiB
    n_lines = 16_000
    content = "".join(f"[agent] line {i} " + "x" * 50 + "\n" for i in range(n_lines))
    f.write_text(content, encoding="utf-8")
    size = f.stat().st_size
    assert size > 1 * 1024 * 1024

    result, new_offset = _read_new_text_lines(f, offset=0)

    assert len(result) > 0
    # Dernière ligne = fin du fichier
    assert f"line {n_lines - 1}" in result[-1]
    # Début du fichier absent des résultats
    assert not any("line 0 " in line for line in result)
    # Offset à la fin
    assert new_offset == size


async def test_s_calls_launch_daemon(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    calls: list[dict] = []

    class _Result:
        launched = True
        pid = 777

    def _fake_launch(**kwargs):
        calls.append(kwargs)
        return _Result()

    monkeypatch.setattr("trader.interfaces.cockpit.supervisor.launch_daemon", _fake_launch)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
    assert len(calls) == 1
    assert calls[0]["pid_file"] == tmp_path / "daemon.pid"
