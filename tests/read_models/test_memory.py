"""TDD — read-model mémoire (learnings.db + ledger + situation + sync)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.agent.learnings.store import LearningsStore
from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore
from trader.reporting.read_models.memory import (
    NOTE_EXCERPT_CHARS,
    compute_global_rules,
    compute_memory_health,
    compute_memory_report,
    compute_note_ranks,
    compute_recall_coverage,
    compute_recall_utility,
    compute_situation_stats,
    compute_store_stats,
    compute_sync_health,
)
from trader.reporting.read_models.runtime_state import load_runtime_state


UTC = timezone.utc
NOW = datetime(2026, 8, 16, 10, 14, tzinfo=UTC)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _seed_learnings(db_path: Path) -> LearningsStore:
    store = LearningsStore(db_path)
    _write_jsonl(
        db_path.with_name("notes.jsonl"),
        [
            {
                "decision_id": "d-win-a",
                "ts": "2026-07-01T10:00:00+00:00",
                "symbol": "NVDA",
                "note": "Breakout held through the first pullback and paid 1R cleanly.",
                "source": "runtime",
            },
            {
                "decision_id": "d-win-b",
                "ts": "2026-07-02T10:00:00+00:00",
                "symbol": "AAPL",
                "note": "Wait for a deeper pullback then buy the reclaimed VWAP.",
                "source": "runtime",
            },
            {
                "decision_id": "d-loss-a",
                "ts": "2026-07-03T10:00:00+00:00",
                "symbol": "2330.TW",
                "note": "Chased strength into resistance and got faded immediately.",
                "source": "ledger-backfill",
            },
            {
                "decision_id": "d-neutral",
                "ts": "2026-07-04T10:00:00+00:00",
                "symbol": "MSFT",
                "note": "Hold was uneventful; no edge either way on the day.",
                "source": "ledger-backfill",
            },
            {
                "decision_id": "d-unknown",
                "ts": "2026-07-05T10:00:00+00:00",
                "symbol": "SPY",
                "note": "Pending outcome, still inside the evaluation window.",
                "source": "consolidated",
            },
        ],
    )
    result = store.ingest_jsonl(db_path.with_name("notes.jsonl"), source="runtime")
    assert result["inserted"] == 5
    # ingest_jsonl overwrites source with the argument; restore per-row sources.
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE notes SET source='runtime' WHERE decision_id IN ('d-win-a','d-win-b')")
    conn.execute("UPDATE notes SET source='ledger-backfill' WHERE decision_id IN ('d-loss-a','d-neutral')")
    conn.execute("UPDATE notes SET source='consolidated' WHERE decision_id='d-unknown'")
    conn.execute("UPDATE notes SET family='us_tech' WHERE symbol IN ('NVDA','AAPL','MSFT')")
    conn.execute("UPDATE notes SET family='tw_semiconductors' WHERE symbol='2330.TW'")
    conn.execute("UPDATE notes SET family='us_index' WHERE symbol='SPY'")
    conn.execute(
        """
        UPDATE notes SET
            verdict=CASE decision_id
                WHEN 'd-win-a' THEN 'WIN'
                WHEN 'd-win-b' THEN 'WIN'
                WHEN 'd-loss-a' THEN 'LOSS'
                WHEN 'd-neutral' THEN 'NEUTRAL'
                ELSE NULL
            END,
            outcome_score=CASE decision_id
                WHEN 'd-win-a' THEN 0.80
                WHEN 'd-win-b' THEN 0.40
                WHEN 'd-loss-a' THEN -0.70
                WHEN 'd-neutral' THEN 0.05
                ELSE NULL
            END,
            q_value=CASE decision_id
                WHEN 'd-win-a' THEN 0.50
                WHEN 'd-win-b' THEN 0.10
                WHEN 'd-loss-a' THEN -0.40
                WHEN 'd-neutral' THEN 0.00
                ELSE NULL
            END,
            q_updates=CASE decision_id
                WHEN 'd-win-a' THEN 12
                WHEN 'd-win-b' THEN 3
                WHEN 'd-loss-a' THEN 20
                WHEN 'd-neutral' THEN 8
                ELSE 0
            END,
            embedding=CASE WHEN decision_id IN ('d-win-a','d-win-b','d-loss-a','d-neutral')
                THEN x'00000000' ELSE NULL END
        """
    )
    conn.execute(
        "INSERT INTO recalls (decision_id, note_ids, ts, reward, evaluated_at) VALUES "
        "('d-llm-buy', '[1]', '2026-07-10T10:00:00+00:00', 1.0, '2026-07-11T10:00:00+00:00'),"
        "('d-llm-hold', '[2]', '2026-07-10T11:00:00+00:00', 1.0, '2026-07-11T11:00:00+00:00'),"
        "('d-llm-sell', '[3]', '2026-07-10T12:00:00+00:00', -1.0, '2026-07-11T12:00:00+00:00'),"
        "('d-llm-flat', '[4]', '2026-07-10T13:00:00+00:00', 0.0, '2026-07-11T13:00:00+00:00'),"
        "('d-pending', '[5]', '2026-07-10T14:00:00+00:00', NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO global_rules (rule_id, q_value, q_updates, active) VALUES "
        "('rule-helps', 0.18, 30, 1),"
        "('rule-hurts', -0.34, 35, 1),"
        "('rule-unknown', 0.50, 2, 1),"
        "('rule-neutral', 0.00, 20, 1),"
        "('rule-retired', 0.90, 40, 0)"
    )
    conn.commit()
    conn.close()
    return store


def _write_ledger(path: Path) -> None:
    _write_jsonl(
        path,
        [
            {
                "decision_id": "d-llm-buy",
                "model_called": True,
                "action": "BUY",
                "decision_source": "llm",
                "cycle_ts": "2026-07-10T10:00:00+00:00",
            },
            {
                "decision_id": "d-llm-hold",
                "model_called": True,
                "action": "HOLD",
                "decision_source": "llm",
                "cycle_ts": "2026-07-10T11:00:00+00:00",
            },
            {
                "decision_id": "d-llm-sell",
                "model_called": True,
                "action": "SELL",
                "decision_source": "llm",
                "cycle_ts": "2026-07-10T12:00:00+00:00",
            },
            {
                "decision_id": "d-llm-flat",
                "model_called": True,
                "action": "HOLD",
                "decision_source": "llm",
                "cycle_ts": "2026-07-10T13:00:00+00:00",
            },
            {
                "decision_id": "d-infra-hold",
                "model_called": False,
                "action": "HOLD",
                "decision_source": "infra",
                "cycle_ts": "2026-07-10T14:00:00+00:00",
            },
            {
                "decision_id": "d-infra-called",
                "model_called": True,
                "action": "HOLD",
                "decision_source": "infra",
                "cycle_ts": "2026-07-10T15:00:00+00:00",
            },
            {
                "decision_id": "d-no-model",
                "model_called": False,
                "action": "BUY",
                "decision_source": "armed_plan",
                "cycle_ts": "2026-07-10T16:00:00+00:00",
            },
        ],
    )


def _seed_situation(db_path: Path) -> None:
    store = SituationMemoryStore(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO situation_notes (note_key, brief_id, point, verdict) VALUES (?, ?, ?, ?)",
        [
            ("k-win", "b1", "bullish industrial tape", "gagnant"),
            ("k-loss", "b1", "faded breakout", "perdant"),
            ("k-open", "b1", "still open", None),
        ],
    )
    conn.commit()
    conn.close()
    store.close()


def _seed_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _seed_learnings(state_dir / "learnings.db")
    _write_ledger(state_dir / "decisions.jsonl")
    _seed_situation(state_dir / "situation_memory.db")
    (state_dir / "learnings_sync_status.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "reason": "post_cycle",
                "as_of": "2026-08-16T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return state_dir


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_stats_compte_sources_verdicts_embeddings(tmp_path: Path) -> None:
    db_path = tmp_path / "learnings.db"
    _seed_learnings(db_path)

    stats = compute_store_stats(db_path)

    assert stats.available is True
    assert stats.n_notes == 5
    assert stats.by_source == (
        ("ledger-backfill", 2),
        ("runtime", 2),
        ("consolidated", 1),
    )
    assert stats.by_verdict == (
        ("WIN", 2),
        ("LOSS", 1),
        ("NEUTRAL", 1),
        ("(none)", 1),
    )
    assert stats.n_with_embedding == 4
    assert stats.pct_with_embedding == pytest.approx(80.0)
    assert stats.n_recalls == 5
    assert stats.n_active_rules == 4
    assert stats.period_from == "2026-07-01T10:00:00+00:00"
    assert stats.period_to == "2026-07-05T10:00:00+00:00"
    assert stats.file_size_bytes == db_path.stat().st_size
    assert stats.file_size_bytes > 0


def test_store_stats_fichier_absent(tmp_path: Path) -> None:
    stats = compute_store_stats(tmp_path / "missing.db")
    assert stats.available is False
    assert stats.n_notes == 0
    assert "introuvable" in (stats.missing_reason or "")


# ---------------------------------------------------------------------------
# Recall coverage / utility
# ---------------------------------------------------------------------------


def test_recall_coverage_joint_llm_authentiques(tmp_path: Path) -> None:
    state_dir = _seed_state_dir(tmp_path)
    extra = sqlite3.connect(str(state_dir / "learnings.db"))
    extra.execute(
        "INSERT INTO recalls (decision_id, note_ids, ts) VALUES "
        "('d-infra-hold', '[1]', '2026-07-10T14:00:00+00:00'),"
        "('d-ghost', '[1]', '2026-07-10T18:00:00+00:00')"
    )
    extra.commit()
    extra.close()

    coverage = compute_recall_coverage(
        state_dir / "learnings.db",
        state_dir / "decisions.jsonl",
    )

    # Authentic LLM: d-llm-buy/hold/sell/flat (not infra HOLD, even if model_called).
    assert coverage.available is True
    assert coverage.n_authentic_llm == 4
    assert coverage.n_with_recall == 4
    assert coverage.pct == pytest.approx(100.0)


def test_recall_coverage_journal_absent(tmp_path: Path) -> None:
    db_path = tmp_path / "learnings.db"
    _seed_learnings(db_path)
    coverage = compute_recall_coverage(db_path, tmp_path / "nope.jsonl")
    assert coverage.available is False
    assert "introuvable" in (coverage.missing_reason or "")


def test_recall_utility_lift_contre_base_rate(tmp_path: Path) -> None:
    utility = compute_recall_utility(_seed_learnings(tmp_path / "learnings.db")._db_path)

    assert utility.available is True
    assert utility.n_evaluated == 4
    assert utility.n_plus == 2
    assert utility.n_zero == 1
    assert utility.n_minus == 1
    assert utility.useful_rate == pytest.approx(2 / 3)
    assert utility.base_rate == pytest.approx(2 / 3)
    assert utility.lift == pytest.approx(0.0)


def test_recall_utility_lift_positif(tmp_path: Path) -> None:
    db_path = tmp_path / "learnings.db"
    _seed_learnings(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE recalls SET reward=1.0 WHERE reward IS NOT NULL")
    conn.commit()
    conn.close()

    utility = compute_recall_utility(db_path)
    assert utility.n_plus == 4
    assert utility.n_minus == 0
    assert utility.useful_rate == pytest.approx(1.0)
    assert utility.base_rate == pytest.approx(2 / 3)
    assert utility.lift == pytest.approx(1.0 - 2 / 3)


# ---------------------------------------------------------------------------
# Notes / rules / situation / sync
# ---------------------------------------------------------------------------


def test_note_ranks_top_flop_score_et_q(tmp_path: Path) -> None:
    ranks = compute_note_ranks(_seed_learnings(tmp_path / "learnings.db")._db_path, limit=2)

    assert [row.symbol for row in ranks.top_outcome] == ["NVDA", "AAPL"]
    assert [row.symbol for row in ranks.flop_outcome] == ["2330.TW", "MSFT"]
    assert [row.symbol for row in ranks.top_q] == ["NVDA", "AAPL"]
    assert [row.symbol for row in ranks.flop_q] == ["2330.TW", "MSFT"]
    assert ranks.top_outcome[0].family == "us_tech"
    assert ranks.top_outcome[0].q_updates == 12
    assert len(ranks.top_outcome[0].excerpt) <= NOTE_EXCERPT_CHARS
    assert "Breakout held" in ranks.top_outcome[0].excerpt


def test_global_rules_recalcule_citation_utility(tmp_path: Path) -> None:
    rules = compute_global_rules(_seed_learnings(tmp_path / "learnings.db")._db_path)
    by_id = {row.rule_id: row for row in rules}

    assert set(by_id) == {"rule-helps", "rule-hurts", "rule-unknown", "rule-neutral"}
    assert "rule-retired" not in by_id
    assert by_id["rule-helps"].utility == "helps"
    assert by_id["rule-hurts"].utility == "hurts"
    assert by_id["rule-unknown"].utility == "unknown"
    assert by_id["rule-neutral"].utility == "neutral"
    assert by_id["rule-helps"].q_updates == 30


def test_situation_stats_repartition_verdicts(tmp_path: Path) -> None:
    db_path = tmp_path / "situation_memory.db"
    _seed_situation(db_path)
    stats = compute_situation_stats(db_path)

    assert stats.available is True
    assert stats.n_notes == 3
    assert stats.n_evaluated == 2
    assert stats.by_verdict == (("gagnant", 1), ("perdant", 1), ("(none)", 1))


def test_situation_stats_fichier_absent(tmp_path: Path) -> None:
    stats = compute_situation_stats(tmp_path / "missing.db")
    assert stats.available is False
    assert "introuvable" in (stats.missing_reason or "")


def test_sync_health_age_depuis_as_of(tmp_path: Path) -> None:
    path = tmp_path / "learnings_sync_status.json"
    path.write_text(
        json.dumps({"status": "ok", "as_of": "2026-08-16T10:00:00+00:00"}),
        encoding="utf-8",
    )
    health = compute_sync_health(path, now=NOW)
    assert health.available is True
    assert health.status == "ok"
    assert health.as_of == "2026-08-16T10:00:00+00:00"
    assert health.age_seconds == pytest.approx(14 * 60)


def test_sync_health_fichier_absent(tmp_path: Path) -> None:
    health = compute_sync_health(tmp_path / "missing.json", now=NOW)
    assert health.available is False
    assert "introuvable" in (health.missing_reason or "")


# ---------------------------------------------------------------------------
# Rapport assemblé + snapshot cockpit + runtime_state
# ---------------------------------------------------------------------------


def test_compute_memory_report_assemble_toutes_les_sections(tmp_path: Path) -> None:
    report = compute_memory_report(_seed_state_dir(tmp_path), now=NOW)

    assert report.store.n_notes == 5
    assert report.coverage.n_authentic_llm == 4
    assert report.utility.n_evaluated == 4
    assert len(report.ranks.top_outcome) == 4
    assert report.rules[0].rule_id == "rule-helps"
    assert report.situation.n_notes == 3
    assert report.health.age_seconds == pytest.approx(14 * 60)


def test_compute_memory_health_snapshot_compact_sans_coverage(tmp_path: Path) -> None:
    snapshot = compute_memory_health(_seed_state_dir(tmp_path), now=NOW)

    assert snapshot["store_available"] is True
    assert snapshot["n_notes"] == 5
    assert snapshot["lift"] == pytest.approx(0.0)
    assert snapshot["useful_rate"] == pytest.approx(2 / 3)
    assert snapshot["base_rate"] == pytest.approx(2 / 3)
    assert snapshot["n_evaluated_recalls"] == 4
    assert snapshot["sync_status"] == "ok"
    assert snapshot["sync_as_of"] == "2026-08-16T10:00:00+00:00"
    rule_ids = {row["rule_id"] for row in snapshot["active_rules"]}
    assert rule_ids == {"rule-helps", "rule-hurts", "rule-unknown", "rule-neutral"}
    assert "n_authentic_llm" not in snapshot


def test_compute_memory_health_tolere_tout_absent(tmp_path: Path) -> None:
    snapshot = compute_memory_health(tmp_path / "empty", now=NOW)
    assert snapshot["store_available"] is False
    assert snapshot["n_notes"] is None
    assert snapshot["lift"] is None
    assert snapshot["active_rules"] == []
    assert snapshot["sync_available"] is False


def test_load_runtime_state_injecte_memory_health(tmp_path: Path) -> None:
    state_dir = _seed_state_dir(tmp_path)
    (state_dir / "current_report.json").write_text(
        '{"ts":"current","portfolio":{"holdings":[]}}',
        encoding="utf-8",
    )
    state = load_runtime_state(
        state_dir=state_dir,
        current_report_path=state_dir / "current_report.json",
        last_report_path=state_dir / "missing_last.json",
        status_path=state_dir / "missing_status.json",
        config_dir=str(tmp_path),
    )
    assert state["memory_health"]["n_notes"] == 5
    assert state["memory_health"]["store_available"] is True


def test_memory_cli_fichiers_absents_sans_traceback(tmp_path: Path, capsys) -> None:
    from scripts.decisions_analytics import memory

    memory(tmp_path)
    out = capsys.readouterr().out
    assert "=== Store ===" in out
    assert "introuvable" in out
    assert "Traceback" not in out
