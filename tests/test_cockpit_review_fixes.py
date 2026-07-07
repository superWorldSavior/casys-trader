"""Tests des quatre findings de review Codex (fact-checkés RÉELS).

1. derive.journal_entries — dédup par identité complète (sequence incluse)
2. derive.next_to_fire    — symbols_due liste de strings → bon compte
3. format.countdown       — datetime naïf ne lève pas TypeError
4. first_run              — has_state_history + escape dismiss
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.derive import journal_entries, next_to_fire
from trader.interfaces.cockpit.first_run import FirstRunScreen, has_state_history

UTC = timezone.utc


# ---------------------------------------------------------------------------
# helpers (repris de test_cockpit_shell.py)
# ---------------------------------------------------------------------------


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")


# ---------------------------------------------------------------------------
# 1. journal_entries — dédup par identité complète
# ---------------------------------------------------------------------------


def test_journal_dedup_by_full_identity():
    """Deux décisions même cycle/symbol/action, sequences 1 et 2 → 2 entrées."""
    NOW = datetime(2026, 7, 6, 2, 1, 0, tzinfo=UTC)
    state = {
        "decisions": [
            {
                "cycle_ts": "2026-07-06T02:00:00+00:00",
                "sequence": "1",
                "symbol": "AAPL",
                "action": "BUY",
                "rationale": "first opportunity",
            },
            {
                "cycle_ts": "2026-07-06T02:00:00+00:00",
                "sequence": "2",
                "symbol": "AAPL",
                "action": "BUY",
                "rationale": "second opportunity",
            },
        ],
        "recent_decisions": [],
    }
    entries = journal_entries(state, now=NOW)
    assert len(entries) == 2, f"expected 2 entries, got {len(entries)}"
    rationales = {e.rationale for e in entries}
    assert "first opportunity" in rationales
    assert "second opportunity" in rationales


def test_journal_dedup_still_deduplicates_true_duplicates():
    """Entrée strictement identique (même sequence/rationale) → 1 seule entrée."""
    NOW = datetime(2026, 7, 6, 2, 1, 0, tzinfo=UTC)
    row = {
        "cycle_ts": "2026-07-06T02:00:00+00:00",
        "sequence": "1",
        "symbol": "AAPL",
        "action": "HOLD",
        "rationale": "same reasoning",
    }
    state = {"decisions": [row], "recent_decisions": [row]}
    entries = journal_entries(state, now=NOW)
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# 2. next_to_fire — symbols_due liste de strings
# ---------------------------------------------------------------------------


def test_next_to_fire_symbols_due_string_list_count():
    """symbols_due = liste de strings → le libellé affiche le bon N."""
    NOW = datetime(2026, 7, 6, 2, 0, 0, tzinfo=UTC)
    state = {
        "default_next_wake": "2026-07-06T04:00:00+00:00",
        "symbols_due": ["AAPL", "MSFT", "GOOG"],
        "symbol_wakes": {},
        "indicator_watches": [],
    }
    items = next_to_fire(state, now=NOW)
    wake_items = [i for i in items if i.kind == "wake"]
    assert wake_items, "aucun item 'wake' trouvé"
    assert "3 symbols due" in wake_items[0].detail, wake_items[0].detail


def test_next_to_fire_symbols_due_empty_list():
    NOW = datetime(2026, 7, 6, 2, 0, 0, tzinfo=UTC)
    state = {
        "default_next_wake": "2026-07-06T04:00:00+00:00",
        "symbols_due": [],
        "symbol_wakes": {},
        "indicator_watches": [],
    }
    items = next_to_fire(state, now=NOW)
    wake_items = [i for i in items if i.kind == "wake"]
    assert wake_items
    assert wake_items[0].detail == "scheduler"


# ---------------------------------------------------------------------------
# 3. format.countdown — datetime naïf ne lève pas TypeError
# ---------------------------------------------------------------------------


def test_countdown_naive_target_no_typeerror():
    """Target naïf + now aware → pas de TypeError, résultat exploitable."""
    naive_target = datetime(2026, 7, 6, 5, 0, 0)  # pas de tzinfo
    now_aware = datetime(2026, 7, 6, 2, 0, 0, tzinfo=UTC)
    result = f.countdown(naive_target, now=now_aware)
    assert result not in ("—", ""), f"résultat inattendu : {result!r}"
    assert "h" in result or "m" in result, f"format inattendu : {result!r}"


def test_countdown_naive_expired_no_typeerror():
    """Target naïf dans le passé → 'expired', pas TypeError."""
    naive_past = datetime(2026, 7, 6, 1, 0, 0)  # avant now
    now_aware = datetime(2026, 7, 6, 2, 0, 0, tzinfo=UTC)
    assert f.countdown(naive_past, now=now_aware) == "expired"


# ---------------------------------------------------------------------------
# 4a. has_state_history
# ---------------------------------------------------------------------------


def test_has_state_history_empty_dir(tmp_path):
    assert has_state_history(tmp_path) is False


def test_has_state_history_decisions_jsonl(tmp_path):
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    assert has_state_history(tmp_path) is True


def test_has_state_history_broker_json(tmp_path):
    (tmp_path / "broker.json").write_text("{}", encoding="utf-8")
    assert has_state_history(tmp_path) is True


def test_has_state_history_last_report(tmp_path):
    (tmp_path / "last_report.json").write_text("{}", encoding="utf-8")
    assert has_state_history(tmp_path) is True


def test_has_state_history_trade_plans(tmp_path):
    (tmp_path / "trade_plans.json").write_text("[]", encoding="utf-8")
    assert has_state_history(tmp_path) is True


def test_has_state_history_casys_db(tmp_path):
    (tmp_path / "casys.db").write_bytes(b"SQLite format 3\0")
    assert has_state_history(tmp_path) is True


# ---------------------------------------------------------------------------
# 4b. FirstRunScreen — escape → dismiss (cockpit normal apparaît)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_escape_dismisses_to_main_screen(tmp_path, monkeypatch):
    """Appui sur escape sur FirstRunScreen → quitte l'écran preflight sans démarrer."""
    _patch_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, FirstRunScreen), "FirstRunScreen attendu au départ"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, FirstRunScreen), (
            f"FirstRunScreen toujours là après escape : {type(app.screen)}"
        )
