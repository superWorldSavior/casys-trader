from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from trader.domain.market_data import Bar
from trader.infrastructure.state_db.learnings_store import LearningsStore
from trader.runtime.learnings_sync_runtime import (
    LearningSyncRunner,
    run_learning_sync,
    score_outcome,
)


UTC = timezone.utc


def _bar(ts: datetime, close: float) -> Bar:
    return Bar(
        ts=ts.isoformat(),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100.0,
    )


def _bars(start: datetime, *, initial: float = 100.0, final: float = 102.0) -> list[Bar]:
    return [
        _bar(start - timedelta(hours=1), initial),
        _bar(start, initial),
        _bar(start + timedelta(hours=4), (initial + final) / 2),
        _bar(start + timedelta(days=1), final),
    ]


def _write_jsonl(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_score_outcome_utilise_1d_puis_mappe_la_reward() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    result = score_outcome(
        {"ts": started.isoformat(), "symbol": "SPY", "action": "BUY"},
        _bars(started, final=103.0),
        now=started + timedelta(days=2),
    )

    assert result is not None
    assert result["verdict"] == "WIN"
    assert result["reward"] == 1.0
    assert result["forward_return"] == pytest.approx(0.03)


def test_score_outcome_hold_long_evalue_le_maintien_de_l_exposition() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    result = score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            "portfolio_snapshot": {
                "holdings": [{"symbol": "SPY", "quantity": 10.0}],
            },
        },
        _bars(started, final=103.0),
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": "WIN",
        "reward": 1.0,
        "forward_return": pytest.approx(0.03),
    }


def test_score_outcome_hold_short_evalue_le_maintien_de_l_exposition() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    result = score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            "portfolio_snapshot": {
                "holdings": [{"symbol": "SPY", "quantity": -10.0}],
            },
        },
        _bars(started, final=97.0),
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": "WIN",
        "reward": 1.0,
        "forward_return": pytest.approx(-0.03),
    }


def test_score_outcome_hold_flat_garde_le_jugement_d_opportunite() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    result = score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            "portfolio_snapshot": {
                "holdings": [{"symbol": "QQQ", "quantity": 5.0}],
            },
        },
        _bars(started, final=103.0),
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": "LOSS",
        "reward": -1.0,
        "forward_return": pytest.approx(0.03),
    }


@pytest.mark.parametrize(
    "snapshot_fields",
    [
        {},
        {"portfolio_snapshot": {}},
        {"portfolio_snapshot": {"holdings": [{}]}},
        {
            "portfolio_snapshot": {
                "holdings": [{"symbol": "SPY", "quantity": "invalid"}],
            },
        },
    ],
)
def test_score_outcome_hold_sans_snapshot_fiable_reste_pending(
    snapshot_fields: dict,
) -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)

    assert score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            **snapshot_fields,
        },
        _bars(started, final=103.0),
        now=started + timedelta(days=4),
    ) is None


def test_sync_ingere_vectorise_et_score_les_nouvelles_notes(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    _write_jsonl(
        state_dir / "learnings.jsonl",
        [
            {
                "decision_id": "note-decision",
                "ts": started.isoformat(),
                "symbol": "SPY",
                "action": "BUY",
                "note": "breakout confirme",
            }
        ],
    )

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=2),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=103.0),
        include_outcomes=True,
        apply_bootstrap=True,
        api_key="test",
        embedder=lambda texts, **_kwargs: [np.ones(1536, dtype=np.float32).tobytes() for _ in texts],
    )

    assert result["ingest"]["runtime"]["inserted"] == 1
    assert result["embeddings_backfilled"] == 1
    assert result["outcomes"]["notes_updated"] == 1
    conn = sqlite3.connect(state_dir / "learnings.db")
    row = conn.execute("SELECT verdict, forward_return, embedding FROM notes").fetchone()
    conn.close()
    assert row[0] == "WIN"
    assert row[1] == pytest.approx(0.03)
    assert row[2] is not None


def test_sync_applique_la_reward_memrl_aux_notes_rappelees(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    _write_jsonl(
        state_dir / "learnings.jsonl",
        [
            {
                "decision_id": "source-note",
                "ts": started.isoformat(),
                "symbol": "SPY",
                "action": "BUY",
                "note": "retest propre",
            }
        ],
    )
    store = LearningsStore(state_dir / "learnings.db")
    store.ingest_jsonl(state_dir / "learnings.jsonl", source="runtime")
    note_id = store._conn.execute("SELECT id FROM notes").fetchone()[0]
    recalled_decision_id = "recalled-decision"
    store.record_recall(decision_id=recalled_decision_id, note_ids=[note_id])
    _write_jsonl(
        state_dir / "decisions.jsonl",
        [
            {
                "decision_id": recalled_decision_id,
                "cycle_ts": started.isoformat(),
                "symbol": "SPY",
                "action": "BUY",
            }
        ],
    )

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=2),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=103.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )

    assert result["outcomes"]["recalls_updated"] == 1
    assert result["outcomes"]["q_values_updated"] == 1
    conn = sqlite3.connect(state_dir / "learnings.db")
    q_value, q_updates = conn.execute("SELECT q_value, q_updates FROM notes").fetchone()
    verdict, reward = conn.execute("SELECT verdict, reward FROM recalls").fetchone()
    conn.close()
    assert (q_value, q_updates) == (0.1, 1)
    assert (verdict, reward) == ("WIN", 1.0)


def test_opening_learning_stays_pending_until_its_lot_is_fully_closed(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    decision_id = "2026-07-01T10:00:00+00:00|0|SPY"
    _write_jsonl(
        state_dir / "learnings.jsonl",
        [{
            "decision_id": decision_id,
            "ts": started.isoformat(),
            "symbol": "SPY",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
            "note": "breakout entry",
        }],
    )
    _write_jsonl(
        state_dir / "model_performance.jsonl",
        [
            {"ts": started.isoformat(), "symbol": "SPY", "action": "BUY", "quantity": 10, "price": 100, "commission": 1, "fx_rate": 1, "decision_id": decision_id},
            {"ts": (started + timedelta(days=1)).isoformat(), "symbol": "SPY", "action": "SELL", "quantity": 4, "price": 110, "commission": 0.4, "fx_rate": 1, "decision_id": "exit-1"},
        ],
    )

    first = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=3),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=130.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )
    assert first["outcomes"]["notes_updated"] == 0

    with (state_dir / "model_performance.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": (started + timedelta(days=2)).isoformat(), "symbol": "SPY", "action": "SELL", "quantity": 6, "price": 105, "commission": 0.6, "fx_rate": 1, "decision_id": "exit-2"}) + "\n")
    second = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=4),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=130.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )
    assert second["outcomes"]["notes_updated"] == 1
    conn = sqlite3.connect(state_dir / "learnings.db")
    verdict, net_return = conn.execute("SELECT verdict, forward_return FROM notes").fetchone()
    conn.close()
    assert verdict == "WIN"
    assert net_return == pytest.approx(0.068)


def test_runner_enchaine_les_micro_batches_sans_nouveau_trigger(tmp_path) -> None:
    calls: list[bool] = []

    def sync_fn(**kwargs):
        calls.append(bool(kwargs["include_outcomes"]))
        return {
            "total_notes": 100,
            "embeddings_backfilled": 64 if len(calls) == 1 else 36,
            "outcomes": {"notes_updated": 0} if kwargs["include_outcomes"] else None,
            "more_work": len(calls) == 1,
            "more_outcomes": False,
        }

    runner = LearningSyncRunner(
        state_dir=tmp_path,
        get_bars=None,
        now_fn=lambda: datetime(2026, 7, 10, 10, tzinfo=UTC),
        sync_fn=sync_fn,
    )

    triggered = runner.trigger(reason="test", force=True)
    triggered["_thread"].join(timeout=2)

    assert calls == [True, False]
    assert runner.status()["running"] is False
