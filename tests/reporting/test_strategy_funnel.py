from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.infrastructure.state_db.connection import (
    StateDb,
    close_all_state_dbs,
)
from trader.infrastructure.state_db.migrations import (
    BROKER_MIGRATION,
    PROCESS_TRACE_MIGRATION,
)
from trader.reporting.read_models.strategy_funnel import (
    StrategyFunnelSourceError,
    compute_strategy_funnel,
    load_strategy_funnel,
)


def _decision(
    decision_id: str,
    ts: str,
    *,
    action: str = "HOLD",
    watch_id: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "cycle_ts": ts,
        "decision_id": decision_id,
        "decision_source": "llm",
        "model_called": True,
        "action": action,
        "rationale": "Decision exploitable avec contexte et invalidation.",
    }
    if watch_id is not None:
        row["decision"] = {
            "indicator_watch": {"id": watch_id, "on_trigger": "WAKE"}
        }
        row["runtime"] = {"indicator_watch_created": True}
    return row


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_load_reads_gzip_archives_and_uses_a_fixed_as_of(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    with gzip.open(archive / "decisions-2026-06.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                _decision(
                    "watch-decision",
                    "2026-06-11T14:00:00+00:00",
                    watch_id="SPY:watch-1",
                )
            )
            + "\n"
        )
    with gzip.open(archive / "events-2026-06.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": "2026-06-11T14:15:00+00:00",
                    "event": "indicator_watch_triggered",
                    "watch_id": "SPY:watch-1",
                }
            )
            + "\n"
        )
    (tmp_path / "broker.json").write_text(
        json.dumps({"cash": 100_000.0, "positions": {}, "fills": []}),
        encoding="utf-8",
    )

    now = datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)
    funnel = load_strategy_funnel(tmp_path, hours=2.0, now=now)

    assert funnel.llm_reviews == 1
    assert funnel.watches_created == 1
    assert funnel.watches_triggered == 1
    assert funnel.since == "2026-06-11T13:00:00+00:00"
    assert funnel.until == "2026-06-11T15:00:00+00:00"


def test_load_fails_closed_on_a_malformed_canonical_ledger(tmp_path: Path) -> None:
    (tmp_path / "decisions.jsonl").write_text("{not-json}\n", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "broker.json").write_text(
        json.dumps({"cash": 100_000.0, "positions": {}, "fills": []}),
        encoding="utf-8",
    )

    with pytest.raises(StrategyFunnelSourceError, match="invalid canonical ledger row"):
        load_strategy_funnel(tmp_path)


def test_load_fails_closed_when_broker_fills_is_not_a_list(tmp_path: Path) -> None:
    (tmp_path / "decisions.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "broker.json").write_text(
        json.dumps({"cash": 100_000.0, "positions": {}, "fills": {}}),
        encoding="utf-8",
    )

    with pytest.raises(StrategyFunnelSourceError, match="fills_not_list"):
        load_strategy_funnel(tmp_path)


def test_load_fails_closed_when_a_broker_fill_is_not_an_object(tmp_path: Path) -> None:
    (tmp_path / "decisions.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "broker.json").write_text(
        json.dumps({"cash": 100_000.0, "positions": {}, "fills": ["bad"]}),
        encoding="utf-8",
    )

    with pytest.raises(StrategyFunnelSourceError, match="fill_not_object"):
        load_strategy_funnel(tmp_path)


def test_causal_funnel_keeps_trigger_expiry_and_direct_branches_separate() -> None:
    decisions = [
        _decision(
            "create-trigger",
            "2026-06-11T14:00:00+00:00",
            watch_id="SPY:trigger",
        ),
        _decision(
            "create-expiry",
            "2026-06-11T14:01:00+00:00",
            watch_id="SPY:expiry",
        ),
        _decision("trigger-trade", "2026-06-11T14:16:00+00:00", action="BUY"),
        _decision("expiry-trade", "2026-06-11T14:31:00+00:00", action="SELL"),
        _decision("direct-trade", "2026-06-11T14:40:00+00:00", action="BUY"),
    ]
    events = [
        {
            "ts": "2026-06-11T14:15:00+00:00",
            "event": "indicator_watch_triggered",
            "watch_id": "SPY:trigger",
        },
        {
            "ts": "2026-06-11T14:30:00+00:00",
            "event": "indicator_watch_expired",
            "watch_id": "SPY:expiry",
        },
    ]
    process_events = [
        {
            "ts": "2026-06-11T13:59:00+00:00",
            "process_instance_id": "coverage-baseline",
            "attempt_id": "coverage-baseline",
            "work_object_key": "SPY",
            "caused_by": [],
            "effect_refs": [],
        },
        {
            "ts": "2026-06-11T14:15:01+00:00",
            "process_instance_id": "process-trigger",
            "attempt_id": "attempt-trigger",
            "work_object_key": "SPY",
            "caused_by": [{"type": "indicator_trigger", "watch_id": "SPY:trigger"}],
            "effect_refs": [
                {
                    "type": "decision",
                    "decision_id": "trigger-trade",
                    "process_instance_id": "process-trigger",
                    "attempt_id": "attempt-trigger",
                }
            ],
        },
        {
            "ts": "2026-06-11T14:30:01+00:00",
            "process_instance_id": "process-expiry",
            "attempt_id": "attempt-expiry",
            "work_object_key": "SPY",
            "caused_by": [
                {
                    "type": "wake_reason",
                    "reason": "watch_expired",
                    "watch_id": "SPY:expiry",
                }
            ],
            "effect_refs": [
                {
                    "type": "decision",
                    "decision_id": "expiry-trade",
                    "process_instance_id": "process-expiry",
                    "attempt_id": "attempt-expiry",
                }
            ],
        },
    ]
    fills = [
        {
            "ts": "2026-06-11T14:16:01+00:00",
            "symbol": "SPY",
            "decision_id": "trigger-trade",
            "process_instance_id": "process-trigger",
            "attempt_id": "attempt-trigger",
        },
        {
            "ts": "2026-06-11T14:31:01+00:00",
            "symbol": "SPY",
            "decision_id": "expiry-trade",
            "process_instance_id": "process-expiry",
            "attempt_id": "attempt-expiry",
        },
        {
            "ts": "2026-06-11T14:40:01+00:00",
            "symbol": "SPY",
            "decision_id": "direct-trade",
        },
    ]

    funnel = compute_strategy_funnel(
        decisions=decisions,
        events=events,
        fills=fills,
        process_events=process_events,
    )

    assert funnel.watches_triggered == 1
    assert funnel.watches_expired_untriggered == 1
    assert funnel.trigger_decisions == 1
    assert funnel.trigger_fills == 1
    assert funnel.expiry_review_decisions == 1
    assert funnel.expiry_review_fills == 1
    assert funnel.direct_trade_decisions == 1
    assert funnel.direct_fills == 1
    assert funnel.causal_coverage_start == "2026-06-11T13:59:00+00:00"


def test_causal_metrics_are_unavailable_without_any_process_event() -> None:
    funnel = compute_strategy_funnel(
        decisions=[
            _decision("direct-trade", "2026-06-11T14:00:00+00:00", action="BUY")
        ],
        events=[],
        fills=[],
        process_events=[],
    )

    assert funnel.causal_linkage_available is False
    assert funnel.causal_coverage_start is None
    assert funnel.direct_trade_decisions is None
    assert funnel.trigger_without_process is None


def test_causal_metrics_fail_closed_when_trace_starts_after_activity() -> None:
    funnel = compute_strategy_funnel(
        decisions=[
            _decision(
                "create-watch",
                "2026-06-11T14:00:00+00:00",
                watch_id="SPY:watch",
            )
        ],
        events=[
            {
                "ts": "2026-06-11T14:15:00+00:00",
                "event": "indicator_watch_triggered",
                "watch_id": "SPY:watch",
            }
        ],
        fills=[],
        process_events=[
            {
                "ts": "2026-06-11T14:10:00+00:00",
                "process_instance_id": "late-process",
                "attempt_id": "late-attempt",
                "work_object_key": "SPY",
                "caused_by": [],
                "effect_refs": [],
            }
        ],
    )

    assert funnel.causal_linkage_available is False
    assert funnel.causal_coverage_start == "2026-06-11T14:10:00+00:00"
    assert funnel.trigger_process_attempts is None
    assert funnel.trigger_without_process is None


def test_nonarmed_watch_supersession_is_inferred_and_not_left_pending() -> None:
    funnel = compute_strategy_funnel(
        decisions=[
            _decision("create-old", "2026-06-11T14:00:00+00:00", watch_id="SPY:old"),
            _decision("create-new", "2026-06-11T14:10:00+00:00", watch_id="SPY:new"),
        ],
        events=[],
        fills=[],
    )

    assert funnel.watches_created == 2
    assert funnel.watches_superseded == 1
    assert funnel.watches_pending_or_unresolved == 1


def test_explicit_watch_supersession_event_closes_pending_watch() -> None:
    funnel = compute_strategy_funnel(
        decisions=[
            _decision("create-old", "2026-06-11T14:00:00+00:00", watch_id="SPY:old")
        ],
        events=[
            {
                "ts": "2026-06-11T14:10:00+00:00",
                "event": "indicator_watch_superseded",
                "watch_id": "SPY:old",
                "superseded_by_watch_id": "SPY:new",
            }
        ],
        fills=[],
    )

    assert funnel.watches_superseded == 1
    assert funnel.watches_pending_or_unresolved == 0


def test_terminal_watch_before_new_creation_is_not_inferred_superseded() -> None:
    funnel = compute_strategy_funnel(
        decisions=[
            _decision("create-old", "2026-06-11T14:00:00+00:00", watch_id="SPY:old"),
            _decision("create-new", "2026-06-11T14:10:00+00:00", watch_id="SPY:new"),
        ],
        events=[
            {
                "ts": "2026-06-11T14:05:00+00:00",
                "event": "indicator_watch_triggered",
                "watch_id": "SPY:old",
            }
        ],
        fills=[],
    )

    assert funnel.watches_triggered == 1
    assert funnel.watches_superseded == 0
    assert funnel.watches_pending_or_unresolved == 1


def test_load_uses_sqlite_process_trace_for_exact_attempt_join(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                "create-watch",
                "2026-06-11T14:00:00+00:00",
                watch_id="SPY:watch",
            ),
            _decision("trigger-buy", "2026-06-11T14:16:00+00:00", action="BUY"),
        ],
    )
    _write_jsonl(
        tmp_path / "events.jsonl",
        [
            {
                "ts": "2026-06-11T14:15:00+00:00",
                "event": "indicator_watch_triggered",
                "watch_id": "SPY:watch",
            }
        ],
    )
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([BROKER_MIGRATION, PROCESS_TRACE_MIGRATION])
    with db.transaction() as cur:
        cur.execute(
            """INSERT INTO broker_fills(
                symbol, side, quantity, price, ts,
                process_instance_id, attempt_id, decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SPY",
                "BUY",
                1.0,
                100.0,
                "2026-06-11T14:16:01+00:00",
                "process-1",
                "attempt-1",
                "trigger-buy",
            ),
        )
        cur.execute(
            """INSERT INTO process_events(
                event_id, process_type, process_version, process_instance_id,
                attempt_id, runtime_run_id, work_object_type, work_object_key,
                event_type, ts, caused_by_json, effect_refs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "coverage-baseline",
                "casys-trader.paper-decision",
                "0.1",
                "coverage-process",
                "coverage-attempt",
                "runtime-1",
                "paper-symbol",
                "SPY",
                "attempt_started",
                "2026-06-11T13:59:00+00:00",
                "[]",
                "[]",
            ),
        )
        cur.execute(
            """INSERT INTO process_events(
                event_id, process_type, process_version, process_instance_id,
                attempt_id, runtime_run_id, work_object_type, work_object_key,
                event_type, ts, caused_by_json, effect_refs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "event-1",
                "casys-trader.paper-decision",
                "0.1",
                "process-1",
                "attempt-1",
                "runtime-1",
                "paper-symbol",
                "SPY",
                "attempt_finished",
                "2026-06-11T14:16:02+00:00",
                json.dumps(
                    [{"type": "indicator_trigger", "watch_id": "SPY:watch"}]
                ),
                json.dumps(
                    [
                        {
                            "type": "decision",
                            "decision_id": "trigger-buy",
                            "process_instance_id": "process-1",
                            "attempt_id": "attempt-1",
                        }
                    ]
                ),
            ),
        )
    db.close()

    try:
        funnel = load_strategy_funnel(
            tmp_path,
            now=datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc),
        )
    finally:
        close_all_state_dbs()

    assert funnel.causal_linkage_available is True
    assert funnel.causal_coverage_start == "2026-06-11T13:59:00+00:00"
    assert funnel.trigger_process_attempts == 1
    assert funnel.trigger_decisions == 1
    assert funnel.trigger_orders_submitted == 1
    assert funnel.trigger_fills == 1
