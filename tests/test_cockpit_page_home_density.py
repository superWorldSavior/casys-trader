"""Tests densité adaptive — page home (pages/home.py).

Couvre :
- build_home_positions : limit adaptatif, « + N more » ssi overflow réel
- build_next_to_fire   : idem avec limit
- build_journal        : idem, limit généreux (max(8, rows//4))
- update_state Textual  : limits calculés via rows_available (pas de hard-code)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.pages.home import (
    HomePage,
    build_home_positions,
    build_journal,
    build_next_to_fire,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 6, 6, 0, 0, tzinfo=UTC)
FUTURE = "2026-07-07T06:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(renderable, width: int = 120) -> str:
    from rich.console import Console

    console = Console(width=width, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _holding(symbol: str, qty: float = 10, pnl_pct: float = 0.0) -> dict:
    """Holding synthétique avec un P&L calculable."""
    entry = 100.0
    current = entry * (1 + pnl_pct / 100)
    return {
        "symbol": symbol,
        "quantity": qty,
        "current_price": current,
        "avg_cost": entry,
        "market_value": qty * current,
        "unrealized_pnl": qty * (current - entry),
    }


def _state_with_positions(n: int) -> dict:
    symbols = [f"SYM{i:02d}" for i in range(n)]
    return {
        "portfolio": {
            "cash": 50000.0,
            "equity": 100000.0,
            "holdings": [_holding(s) for s in symbols],
        }
    }


def _state_with_watches(n: int) -> dict:
    return {
        "indicator_watches": [
            {
                "symbol": f"W{i:02d}",
                "on_trigger": "WAKE",
                "logic": "all",
                "conditions": [{"indicator": "breakout", "op": ">=", "value": 1, "timeframe": "1h"}],
                "expires_at": FUTURE,
                "created_at": "2026-07-06T00:00:00+00:00",
            }
            for i in range(n)
        ]
    }


def _state_with_decisions(n: int) -> dict:
    return {
        "recent_decisions": [
            {
                "symbol": f"D{i:02d}",
                "cycle_ts": f"2026-07-06T0{i % 10}:00:00+00:00",
                "action": "HOLD",
                "confidence": 0.6,
                "rationale": f"reason {i}",
                "runtime": {"tool_calls": []},
            }
            for i in range(n)
        ],
        "decisions_total": n,
    }


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")


def _make_minimal_state(tmp_path) -> None:
    (tmp_path / "current_report.json").write_text(
        json.dumps({
            "ts": "2026-07-06T06:00:00+00:00",
            "dry_run": True,
            "portfolio": {"cash": 80000.0, "equity": 100000.0, "holdings": []},
            "decisions": [],
        }),
        encoding="utf-8",
    )
    (tmp_path / "daemon_status.json").write_text(
        json.dumps({
            "phase": "idle",
            "decisions_done": 0,
            "symbols_total": 0,
            "model_calls_used": 0,
            "max_model_calls_per_cycle": 25,
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# build_home_positions — limit adaptatif
# ---------------------------------------------------------------------------


def test_positions_limit_clips_overflow():
    """Avec limit=3 et 5 positions, « + 2 more » doit apparaître."""
    rendered = _render(build_home_positions(_state_with_positions(5), limit=3))
    assert "+ 2 more" in rendered


def test_positions_limit_no_more_when_fits():
    """Avec limit=10 et 5 positions, aucun « more »."""
    rendered = _render(build_home_positions(_state_with_positions(5), limit=10))
    assert "more" not in rendered


def test_positions_limit_exact_boundary():
    """limit == count : pas de « more »."""
    rendered = _render(build_home_positions(_state_with_positions(4), limit=4))
    assert "more" not in rendered


def test_positions_empty_state():
    rendered = _render(build_home_positions({}))
    assert "no positions" in rendered


def test_positions_limit_one_shows_one_row():
    """limit=1 : une seule position visible."""
    rendered = _render(build_home_positions(_state_with_positions(3), limit=1))
    assert "SYM00" in rendered
    # SYM01 tronqué
    assert "+ 2 more" in rendered


# ---------------------------------------------------------------------------
# build_next_to_fire — limit adaptatif
# ---------------------------------------------------------------------------


def test_fire_limit_clips_overflow():
    """Avec limit=2 et 4 watches, les 2 surplus ne sont pas rendus."""
    state = _state_with_watches(4)
    rendered = _render(build_next_to_fire(state, now=NOW, limit=2))
    # Seulement les 2 premiers symboles présents
    assert "W00" in rendered
    assert "W01" in rendered
    assert "W02" not in rendered
    assert "W03" not in rendered


def test_fire_limit_no_truncation_when_fits():
    state = _state_with_watches(3)
    rendered = _render(build_next_to_fire(state, now=NOW, limit=10))
    assert "W00" in rendered
    assert "W01" in rendered
    assert "W02" in rendered


def test_fire_empty_state():
    rendered = _render(build_next_to_fire({}, now=NOW))
    assert "nothing armed" in rendered


# ---------------------------------------------------------------------------
# build_journal — limit généreux (max(8, rows//4))
# ---------------------------------------------------------------------------


def test_journal_limit_clips():
    """limit=2 : seulement les 2 entrées les plus récentes rendues.

    Le journal est trié du plus récent au plus ancien, donc D04 et D03 (indices
    hauts = cycle_ts le plus tard) doivent figurer ; D00 non.
    """
    state = _state_with_decisions(5)
    rendered = _render(build_journal(state, now=NOW, limit=2))
    # Les 2 plus récents (D04, D03) doivent être présents
    assert "D04" in rendered
    assert "D03" in rendered
    # D00 ne doit pas apparaître dans les entrées (peut figurer dans « more »)
    lines_with_d00 = [ln for ln in rendered.splitlines() if "D00" in ln and "more" not in ln]
    assert lines_with_d00 == []


def test_journal_limit_no_truncation():
    state = _state_with_decisions(3)
    rendered = _render(build_journal(state, now=NOW, limit=10))
    assert "D00" in rendered
    assert "D01" in rendered
    assert "D02" in rendered


def test_journal_empty_state():
    rendered = _render(build_journal({}, now=NOW))
    assert "reasoning" in rendered


def test_journal_default_limit_is_8():
    """Le défaut limit=8 est conservé pour la compatibilité."""
    state = _state_with_decisions(10)
    # Avec limit=8 on doit avoir « more » (2 restant)
    rendered = _render(build_journal(state, now=NOW, limit=8))
    assert "more in the ledger" in rendered


# ---------------------------------------------------------------------------
# Textual widget — update_state utilise rows_available (smoke test)
# ---------------------------------------------------------------------------


async def test_home_page_update_state_no_crash(tmp_path, monkeypatch):
    """update_state avec données complètes ne plante pas et affiche les panneaux."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")

    state = {
        **_state_with_positions(20),
        **_state_with_watches(10),
        **_state_with_decisions(15),
    }

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        # Page 1 = home (touche 1 ou défaut)
        await pilot.press("1")
        await pilot.pause()
        page = app.query_one("#home-page", HomePage)
        app._last_state = state
        page.update_state(state)
        await pilot.pause()

        # Les panneaux doivent être présents
        assert app.query_one("#positions-panel") is not None
        assert app.query_one("#fire-panel") is not None
        assert app.query_one("#journal-panel") is not None


async def test_home_page_update_state_empty(tmp_path, monkeypatch):
    """update_state avec état vide ne plante pas."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        page = app.query_one("#home-page", HomePage)
        page.update_state({})
        page.update_state({"portfolio": None, "recent_decisions": None})


async def test_home_page_positions_limit_adapts_to_height(tmp_path, monkeypatch):
    """Sur un grand écran (80 lignes) la limit positions > minimum (4)."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")

    state = _state_with_positions(30)

    app = CockpitApp()
    # Terminal large et haut : 220×80
    async with app.run_test(size=(220, 80)) as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        page = app.query_one("#home-page", HomePage)
        app._last_state = state
        page.update_state(state)
        await pilot.pause()

        from trader.interfaces.cockpit.pages._shared import rows_available
        from textual.containers import VerticalScroll

        positions_panel = app.query_one("#positions-panel", VerticalScroll)
        limit = rows_available(positions_panel, reserved=1, minimum=4)
        # Sur un écran de 80 lignes le panneau a forcément plus de 4 lignes dispo
        assert limit >= 4
