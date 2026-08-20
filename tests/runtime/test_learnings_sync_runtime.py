from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from trader.application.record.learning_outcomes import realised_entry_outcomes
from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION
from trader.domain.market_data import Bar
from trader.domain.situation import NewsMacroBrief
from trader.infrastructure.state_db.connection import open_state_db
from trader.infrastructure.state_db.learnings_store import LearningsStore
from trader.infrastructure.state_db.migrations import import_broker_from_json
from trader.reporting.read_models.trade_history import (
    aggregate_position_cycles,
    compute_round_trips,
)
from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore
from trader.runtime.learnings_sync_runtime import (
    LearningSyncRunner,
    _lookback_for,
    run_learning_sync,
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


def _replace_canonical_fills(state_dir, rows: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    db = open_state_db(state_dir / "casys.db")
    import_broker_from_json(
        db,
        state_dir / "_absent_broker.json",
        starting_cash=100_000.0,
    )
    with db.transaction() as cur:
        cur.execute("DELETE FROM broker_fills")
        cur.executemany(
            """
            INSERT INTO broker_fills(
                symbol, side, quantity, price, ts, commission,
                commission_currency, commission_model, fx_rate, decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.get("symbol"),
                    row.get("side") or row.get("action"),
                    row.get("quantity"),
                    row.get("price"),
                    row.get("ts"),
                    row.get("commission"),
                    row.get("commission_currency"),
                    row.get("commission_model"),
                    row.get("fx_rate"),
                    row.get("decision_id"),
                )
                for row in rows
            ],
        )


def test_historical_outcome_lookback_uses_provider_supported_windows() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)

    assert _lookback_for([], now=now) == "5d"
    assert _lookback_for([{"ts": (now - timedelta(days=20)).isoformat()}], now=now) == "23d"
    assert _lookback_for([{"ts": (now - timedelta(days=31)).isoformat()}], now=now) == "3mo"
    assert _lookback_for([{"ts": (now - timedelta(days=120)).isoformat()}], now=now) == "6mo"


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
    assert result["outcomes"]["universe_selections"]["evaluated"] == 0
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
    performance_rows = [
            {"ts": started.isoformat(), "symbol": "SPY", "action": "BUY", "quantity": 10, "price": 100, "commission": 1, "commission_model": "ibkr_us_stock_tiered", "commission_currency": "USD", "fx_rate": 1, "decision_id": decision_id},
            {"ts": (started + timedelta(days=1)).isoformat(), "symbol": "SPY", "action": "SELL", "quantity": 4, "price": 110, "commission": 0.4, "commission_model": "ibkr_us_stock_tiered", "commission_currency": "USD", "fx_rate": 1, "decision_id": "exit-1"},
    ]
    _write_jsonl(state_dir / "model_performance.jsonl", performance_rows)
    _replace_canonical_fills(state_dir, performance_rows)

    first = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=3),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=130.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )
    assert first["outcomes"]["notes_updated"] == 0

    final_exit = {"ts": (started + timedelta(days=2)).isoformat(), "symbol": "SPY", "action": "SELL", "quantity": 6, "price": 105, "commission": 0.6, "commission_model": "ibkr_us_stock_tiered", "commission_currency": "USD", "fx_rate": 1, "decision_id": "exit-2"}
    with (state_dir / "model_performance.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(final_exit) + "\n")
    _replace_canonical_fills(state_dir, [*performance_rows, final_exit])
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


def test_sync_continues_after_partial_batch_blocked_by_open_position(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    open_decision_id = "2026-07-01T11:00:00+00:00|0|OPEN"
    scored_decision_id = "2026-07-01T10:00:00+00:00|0|SCORED"
    _write_jsonl(
        state_dir / "learnings.jsonl",
        [
            {
                "decision_id": open_decision_id,
                "ts": (started + timedelta(hours=1)).isoformat(),
                "symbol": "OPEN",
                "action": "BUY",
                "intent": "OPEN_LONG",
                "executed": True,
                "note": "position encore ouverte",
            },
            {
                "decision_id": scored_decision_id,
                "ts": started.isoformat(),
                "symbol": "SCORED",
                "action": "BUY",
                "note": "decision evaluable",
            },
        ],
    )

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=2),
        get_bars=lambda symbol, **_kwargs: _bars(
            started + (timedelta(hours=1) if symbol == "OPEN" else timedelta()),
            final=103.0,
        ),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
        outcome_batch_size=2,
    )

    assert result["outcomes"]["pending_notes"] == 2
    assert result["outcomes"]["notes_updated"] == 1
    assert result["more_outcomes"] is True
    assert result["more_work"] is True


def test_sync_scores_hold_candidate_with_portfolio_snapshot_from_ledger(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    decision_id = "2026-07-01T10:00:00+00:00|0|SPY"
    _write_jsonl(
        state_dir / "learnings.jsonl",
        [{
            "decision_id": decision_id,
            "ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            "intent": "HOLD",
            "executed": False,
            "note": "Attendre la confirmation du breakout.",
        }],
    )
    _write_jsonl(
        state_dir / "decisions.jsonl",
        [{
            "decision_id": decision_id,
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            "intent": "HOLD",
            "executed": False,
            "portfolio_snapshot": {"holdings": [{"symbol": "SPY", "quantity": 10.0}]},
        }],
    )

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=2),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=103.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )

    assert result["outcomes"]["notes_updated"] == 1
    conn = sqlite3.connect(state_dir / "learnings.db")
    verdict, forward_return = conn.execute("SELECT verdict, forward_return FROM notes").fetchone()
    conn.close()
    assert verdict == "WIN"
    assert forward_return == pytest.approx(0.03)


def test_v3_migration_replays_executed_opening_from_realised_lot(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    decision_id = "2026-07-01T10:00:00+00:00|0|SPY"
    learning = {
        "decision_id": decision_id,
        "ts": started.isoformat(),
        "symbol": "SPY",
        "action": "BUY",
        "intent": "OPEN_LONG",
        "executed": True,
        "note": "opening to replay",
    }
    _write_jsonl(state_dir / "learnings.jsonl", [learning])
    _write_jsonl(
        state_dir / "decisions.jsonl",
        [{**learning, "cycle_ts": learning["ts"]}],
    )
    performance_rows = [
            {
                "ts": started.isoformat(),
                "symbol": "SPY",
                "action": "BUY",
                "quantity": 10,
                "price": 100,
                "commission": 1,
                "commission_model": "ibkr_us_stock_tiered",
                "commission_currency": "USD",
                "fx_rate": 1,
                "decision_id": decision_id,
            },
            {
                "ts": (started + timedelta(days=1)).isoformat(),
                "symbol": "SPY",
                "action": "SELL",
                "quantity": 10,
                "price": 110,
                "commission": 1,
                "commission_model": "ibkr_us_stock_tiered",
                "commission_currency": "USD",
                "fx_rate": 1,
                "decision_id": "exit",
            },
    ]
    _write_jsonl(state_dir / "model_performance.jsonl", performance_rows)
    _replace_canonical_fills(state_dir, performance_rows)
    store = LearningsStore(state_dir / "learnings.db")
    store.ingest_jsonl(state_dir / "learnings.jsonl", source="runtime")
    note_id = store._conn.execute("SELECT id FROM notes").fetchone()[0]
    store.update_note_outcomes([{
        "id": note_id,
        "verdict": "LOSS",
        "forward_return": -0.05,
    }])
    store._conn.execute("UPDATE notes SET outcome_semantics_version=2")
    store._conn.execute(
        """
        UPDATE learnings_metadata SET value='2'
        WHERE key='outcome_semantics_version'
        """
    )
    store._conn.commit()
    store.close()

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=3),
        get_bars=None,
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )

    conn = sqlite3.connect(state_dir / "learnings.db")
    row = conn.execute(
        """
        SELECT verdict, forward_return, outcome_semantics_version
        FROM notes
        """
    ).fetchone()
    conn.close()
    assert result["outcomes"]["notes_updated"] == 1
    assert row[0] == "WIN"
    assert row[1] == pytest.approx(0.098)
    assert row[2] == BENCHMARK_SEMANTICS_VERSION


def test_v3_migration_keeps_opening_pending_when_sqlite_fees_are_unavailable(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    decision_id = "fee-unavailable-entry"
    learning = {
        "decision_id": decision_id,
        "ts": started.isoformat(),
        "symbol": "SPY",
        "action": "BUY",
        "intent": "OPEN_LONG",
        "executed": True,
        "note": "must remain pending without proven broker fees",
    }
    _write_jsonl(state_dir / "learnings.jsonl", [learning])
    canonical_rows = [
        {
            "ts": started.isoformat(),
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "price": 100,
            "commission": 0.0,
            "commission_model": "none",
            "commission_currency": "USD",
            "fx_rate": 1.0,
        },
        {
            "ts": (started + timedelta(days=1)).isoformat(),
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 1,
            "price": 110,
            "commission": 0.0,
            "commission_model": "none",
            "commission_currency": "USD",
            "fx_rate": 1.0,
        },
    ]
    _replace_canonical_fills(state_dir, canonical_rows)
    # Exact execution matches may contribute decision metadata only.  Their
    # optimistic economics must never override the SQLite broker ledger.
    _write_jsonl(
        state_dir / "model_performance.jsonl",
        [
            {
                **canonical_rows[0],
                "decision_id": decision_id,
                "commission": 0.35,
                "commission_model": "ibkr_us_stock_tiered",
            },
            {
                **canonical_rows[1],
                "decision_id": "fee-unavailable-exit",
                "commission": 0.35,
                "commission_model": "ibkr_us_stock_tiered",
            },
        ],
    )

    store = LearningsStore(state_dir / "learnings.db")
    store.ingest_jsonl(state_dir / "learnings.jsonl", source="runtime")
    note_id = int(store._conn.execute("SELECT id FROM notes").fetchone()[0])
    store.update_note_outcomes(
        [{"id": note_id, "verdict": "WIN", "forward_return": 0.10}]
    )
    store._conn.execute(
        """
        UPDATE notes
        SET outcome_semantics_version=2, q_value=0.7, q_updates=4
        """
    )
    store._conn.execute(
        """
        UPDATE learnings_metadata SET value='2'
        WHERE key='outcome_semantics_version'
        """
    )
    store._conn.commit()
    store.close()

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=3),
        get_bars=None,
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )

    conn = sqlite3.connect(state_dir / "learnings.db")
    row = conn.execute(
        """
        SELECT verdict, forward_return, outcome_semantics_version,
               q_value, q_updates
        FROM notes
        """
    ).fetchone()
    conn.close()
    assert result["outcomes"]["notes_updated"] == 0
    assert row == (None, None, None, 0.0, 0)


def test_realised_outcome_uses_sqlite_fx_not_divergent_projection(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    canonical_rows = [
        {
            "ts": started.isoformat(),
            "symbol": "AIR.PA",
            "action": "BUY",
            "quantity": 10,
            "price": 100,
            "commission": 1.0,
            "commission_model": "ibkr_europe_stock_tiered",
            "commission_currency": "EUR",
            "fx_rate": 1.1,
        },
        {
            "ts": (started + timedelta(days=1)).isoformat(),
            "symbol": "AIR.PA",
            "action": "SELL",
            "quantity": 10,
            "price": 110,
            "commission": 1.0,
            "commission_model": "ibkr_europe_stock_tiered",
            "commission_currency": "EUR",
            "fx_rate": 1.2,
        },
    ]
    _replace_canonical_fills(state_dir, canonical_rows)
    _write_jsonl(
        state_dir / "model_performance.jsonl",
        [
            {
                **canonical_rows[0],
                "decision_id": "eur-entry",
                "fx_rate": 8.0,
                "commission": 99.0,
            },
            {
                **canonical_rows[1],
                "decision_id": "eur-exit",
                "fx_rate": 9.0,
                "commission": 99.0,
            },
        ],
    )

    outcomes = realised_entry_outcomes(
        aggregate_position_cycles(
            compute_round_trips(state_dir, require_canonical=True)
        )
    )

    assert outcomes == {"eur-entry": pytest.approx(217.7 / 1_100.0)}


def test_sync_rejects_legacy_bootstrap_without_blocking_live_maintenance(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    _write_jsonl(
        state_dir / "learnings.jsonl",
        [{
            "decision_id": "pending",
            "ts": started.isoformat(),
            "symbol": "SPY",
            "action": "BUY",
            "note": "pending",
        }],
    )
    bootstrap = state_dir / "archive" / "learnings-outcome-bootstrap.json"
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text(
        json.dumps({
            "learnings": [{
                "decision_id": "pending",
                "verdict": "WIN",
                "forward_return": 0.03,
            }]
        }),
        encoding="utf-8",
    )

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=2),
        get_bars=None,
        include_outcomes=False,
        apply_bootstrap=True,
        api_key="",
    )

    assert result["verdicts_updated"] == 0
    assert str(result["bootstrap_error"]).startswith("ValueError:")
    conn = sqlite3.connect(state_dir / "learnings.db")
    row = conn.execute("SELECT verdict FROM notes").fetchone()
    conn.close()
    assert row[0] is None


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


def _ingest_symbol_note(
    state_dir,
    *,
    as_of: str,
    horizon: str,
    symbol: str = "SPY",
    direction: str = "bullish",
) -> None:
    store = SituationMemoryStore(state_dir / "situation_memory.db")
    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": f"{as_of}|US|{symbol}",
            "venue": "US",
            "as_of": as_of,
            "valid_until": as_of,
            "symbols": {
                symbol: [
                    {
                        "point": f"{symbol} directional call",
                        "sources": ["Reuters"],
                        "source_refs": ["u1"],
                        "symbols": [symbol],
                        "direction": direction,
                        "horizon": horizon,
                    }
                ]
            },
        }
    )
    assert brief is not None
    store.ingest_brief(brief)
    store.close()


def test_sync_evalue_les_notes_situation_matures_sans_figer_les_immatures(tmp_path) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    _ingest_symbol_note(state_dir, as_of=started.isoformat(), horizon="session", symbol="SPY")
    _ingest_symbol_note(
        state_dir,
        as_of=(started + timedelta(minutes=1)).isoformat(),
        horizon="weeks",
        symbol="QQQ",
    )

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=2),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=103.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )

    situation = result["outcomes"]["situation_notes"]
    assert situation["pending"] == 2
    assert situation["evaluated"] == 1
    store = SituationMemoryStore(state_dir / "situation_memory.db")
    by_symbol = {row["section_name"]: row for row in store.load_notes()}
    assert by_symbol["SPY"]["verdict"] == "gagnant"
    assert by_symbol["QQQ"]["verdict"] is None
    assert by_symbol["QQQ"]["evaluated_at"] is None


def test_sync_respecte_opt_out_situation_outcomes(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    _ingest_symbol_note(state_dir, as_of=started.isoformat(), horizon="session")
    monkeypatch.setenv("TRADER_SITUATION_OUTCOMES_ENABLED", "0")

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=2),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=103.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )

    assert result["outcomes"]["situation_notes"]["skipped"] == "disabled"
    store = SituationMemoryStore(state_dir / "situation_memory.db")
    assert store.load_outcomes() == []


def test_sync_logge_les_erreurs_universe_et_situation(tmp_path, caplog, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    SituationMemoryStore(state_dir / "situation_memory.db").close()
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)

    def boom(*_args, **_kwargs):
        raise RuntimeError("judge down")

    monkeypatch.setattr(
        "trader.application.universe.selection_attribution.refresh_selection_outcomes",
        boom,
    )
    monkeypatch.setattr(
        "trader.application.analyst.situation_attribution.refresh_situation_outcomes",
        boom,
    )
    caplog.set_level(logging.WARNING)

    result = run_learning_sync(
        state_dir=state_dir,
        now=started + timedelta(days=2),
        get_bars=lambda *_args, **_kwargs: _bars(started, final=103.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
    )

    assert "judge down" in caplog.text
    assert result["outcomes"]["universe_selections"]["error"].startswith("RuntimeError:")
    assert result["outcomes"]["situation_notes"]["error"].startswith("RuntimeError:")


def test_sync_enchaine_si_le_batch_universe_est_plein(tmp_path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)

    monkeypatch.setattr(
        "trader.application.universe.selection_attribution.refresh_selection_outcomes",
        lambda *_args, **_kwargs: {
            "pending": 128,
            "evaluated": 4,
            "progressed": 4,
            "stored": 4,
        },
    )
    monkeypatch.setattr(
        "trader.application.analyst.situation_attribution.refresh_situation_outcomes",
        lambda *_args, **_kwargs: {"pending": 0, "evaluated": 0, "stored": 0},
    )

    result = run_learning_sync(
        state_dir=state_dir,
        now=started,
        get_bars=lambda *_args, **_kwargs: _bars(started, final=103.0),
        include_outcomes=True,
        apply_bootstrap=False,
        api_key="",
        outcome_batch_size=128,
    )

    assert result["more_outcomes"] is True
    assert result["more_work"] is True


def test_sync_n_enchaine_pas_si_le_batch_universe_est_plein_sans_progres(
    tmp_path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    universe_calls = 0

    def no_progress_then_empty(*_args, **_kwargs):
        nonlocal universe_calls
        universe_calls += 1
        if universe_calls > 1:
            return {
                "pending": 0,
                "evaluated": 0,
                "progressed": 0,
                "stored": 0,
            }
        return {
            "pending": 128,
            "evaluated": 0,
            "progressed": 0,
            "stored": 0,
        }

    monkeypatch.setattr(
        "trader.application.universe.selection_attribution.refresh_selection_outcomes",
        no_progress_then_empty,
    )
    monkeypatch.setattr(
        "trader.application.analyst.situation_attribution.refresh_situation_outcomes",
        lambda *_args, **_kwargs: {"pending": 0, "evaluated": 0, "stored": 0},
    )

    runner = LearningSyncRunner(
        state_dir=state_dir,
        get_bars=lambda *_args, **_kwargs: _bars(started, final=103.0),
        now_fn=lambda: started,
    )
    triggered = runner.trigger(reason="test", force=True)
    triggered["_thread"].join(timeout=2)

    assert universe_calls == 1
    assert runner.status()["running"] is False
