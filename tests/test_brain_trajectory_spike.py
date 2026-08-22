from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import brain_trajectory_spike as spike


def _epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _shared(cycle_id: str, symbol: str) -> dict:
    return {
        "decision_context_scope": {"version": "decision_focus_v1", "symbol": symbol},
        "now": cycle_id,
        "portfolio": {
            "cash": 10_000.0,
            "equity": 20_000.0,
            "gross_exposure_usd": 1_000.0,
            "holdings": [],
        },
        "risk_capacity": {"gross_remaining_usd": 5_000.0, "per_symbol": {}},
        "cockpit": {"focus": {"cols": ["s", "p"], "rows": [[symbol, 100.0]]}},
        "stale_market_data": {},
        "active_plans_summary": {"target": []},
    }


def _create_session(
    root: Path,
    *,
    session_id: str,
    cycle_id: str,
    symbol: str,
    created_at: str,
    shared: dict,
    symbol_facts: dict | None = None,
    semantic_valid: bool = False,
    turn_count: int = 2,
    native_outcome: str = "error",
) -> Path:
    session_dir = root / "encoded-cwd" / session_id
    (session_dir / "prompts").mkdir(parents=True)
    (session_dir / "prompts" / "prompt_0.txt").write_text(
        "# Planner\n\n# Contexte partagé (JSON)\n"
        + json.dumps(shared, ensure_ascii=False, separators=(",", ":"))
        + "\n\n# Symboles à décider (JSON)\n"
        + json.dumps(
            [{"symbol": symbol, **(symbol_facts or {})}],
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (session_dir / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": session_id, "cwd": "/tmp/calc-scratch"},
                "created_at": created_at + "Z",
                "updated_at": created_at + "Z",
                "current_model_id": "grok-4.6",
                "agent_name": "grok-build-plan",
                "reasoning_effort": "low",
                "head_commit": "abc123",
                "head_branch": "main",
                "git_root_dir": str(root.parent),
                "request_id": "request-" + session_id,
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "signals.json").write_text(
        json.dumps(
            {
                "turnCount": turn_count,
                "assistantMessageCount": 3,
                "toolCallCount": 1,
                "toolFailureCount": 1 if native_outcome == "error" else 0,
            }
        ),
        encoding="utf-8",
    )
    native_id = "call-" + session_id
    _write_jsonl(
        session_dir / "events.jsonl",
        [
            {
                "ts": created_at + "Z",
                "type": "turn_started",
                "turn_number": 0,
            },
            {
                "ts": created_at + "Z",
                "type": "tool_completed",
                "tool_name": "read_file",
                "duration_ms": 12,
                "outcome": native_outcome,
                "tool_call_id": native_id,
            },
            {
                "ts": created_at + "Z",
                "type": "turn_ended",
                "outcome": "completed",
            },
        ],
    )
    domain_result = {
        "valid": semantic_valid,
        "evaluation_id": "tpe-ok" if semantic_valid else None,
        "reasons": [] if semantic_valid else [{"code": "risk_pct_out_of_range"}, "invalid_exit_plan"],
    }
    _write_jsonl(
        session_dir / "chat_history.jsonl",
        [
            {
                "type": "user",
                "prompt_index": 0,
                "content": [{"type": "text", "text": "[Full request offloaded to file]"}],
            },
            {
                "type": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": native_id,
                        "name": "read_file",
                        "arguments": json.dumps({"target_file": str(session_dir / "prompts" / "prompt_0.txt")}),
                    }
                ],
                "model_id": "grok-4.6-build",
            },
            {
                "type": "tool_result",
                "tool_call_id": native_id,
                "content": "Failed to read file" if native_outcome == "error" else "ok",
            },
            {
                "type": "assistant",
                "content": json.dumps(
                    {
                        "tool_calls": [
                            {
                                "id": "e1",
                                "tool": "evaluate_trade_plan",
                                "args": {"symbol": symbol, "risk_pct": 0.25},
                            }
                        ]
                    }
                ),
                "model_id": "grok-4.6-build",
            },
            {
                "type": "user",
                "prompt_index": 1,
                "content": [
                    {
                        "type": "text",
                        "text": "<user_query>\n# Nouveaux résultats (JSON)\n"
                        + json.dumps(
                            [
                                {
                                    "symbol": symbol,
                                    "tool_results": [
                                        {
                                            "id": "e1",
                                            "tool": "evaluate_trade_plan",
                                            "ok": True,
                                            "result": domain_result,
                                        }
                                    ],
                                }
                            ]
                        ),
                    }
                ],
            },
            {
                "type": "assistant",
                "content": json.dumps(
                    {
                        "decisions": [
                            {
                                "symbol": symbol,
                                "confidence": 0.5,
                                "decision_reason_code": "NO_EDGE",
                                "rationale": "fixture",
                                "calls": [],
                            }
                        ]
                    }
                ),
                "model_id": "grok-4.6-build",
            },
        ],
    )
    return session_dir


def _create_task_db(path: Path, rows: list[dict]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tasks (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          dedup_key TEXT UNIQUE,
          partition_key TEXT,
          resource TEXT,
          priority INTEGER NOT NULL,
          payload TEXT,
          status TEXT NOT NULL,
          attempts INTEGER DEFAULT 0,
          max_attempts INTEGER DEFAULT 3,
          scheduled_at INTEGER NOT NULL,
          lease_expires_at INTEGER,
          claim_token TEXT,
          claimed_by TEXT,
          enqueued_seq INTEGER,
          parent_id INTEGER,
          result TEXT,
          error TEXT,
          created_at INTEGER,
          updated_at INTEGER
        );
        """
    )
    for row in rows:
        connection.execute(
            """INSERT INTO tasks(
                   id, kind, dedup_key, partition_key, resource, priority,
                   payload, status, attempts, max_attempts, scheduled_at,
                   result, error, created_at, updated_at
               ) VALUES (?, 'decide', ?, ?, 'acpx', 100, ?, ?, ?, 3, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                f"{row['cycle_id']}:{row['symbol']}",
                row["symbol"],
                json.dumps(row["payload"], allow_nan=True),
                row["status"],
                row["attempts"],
                _epoch_ms(row["created_at"]),
                json.dumps(row.get("result")) if row.get("result") is not None else None,
                row.get("error"),
                _epoch_ms(row["created_at"]),
                _epoch_ms(row["updated_at"]),
            ),
        )
    connection.commit()
    connection.close()


def _create_casys_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE process_events (
          seq INTEGER PRIMARY KEY,
          event_id TEXT,
          process_type TEXT,
          process_version TEXT,
          process_instance_id TEXT,
          attempt_id TEXT,
          runtime_run_id TEXT,
          work_object_type TEXT,
          work_object_key TEXT,
          event_type TEXT,
          ts TEXT,
          caused_by_json TEXT,
          terminal_result TEXT,
          outcome_code TEXT,
          effect_status TEXT,
          effect_refs_json TEXT,
          version_pins_json TEXT
        );
        CREATE TABLE broker_fills (
          seq INTEGER PRIMARY KEY,
          symbol TEXT,
          side TEXT,
          quantity REAL,
          price REAL,
          ts TEXT,
          commission REAL,
          commission_currency TEXT,
          commission_model TEXT,
          fx_rate REAL,
          decision_id TEXT,
          process_instance_id TEXT,
          attempt_id TEXT
        );
        CREATE TABLE universe_selection_outcomes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          mandate_id TEXT NOT NULL,
          symbol TEXT NOT NULL,
          family TEXT NOT NULL DEFAULT '',
          role TEXT NOT NULL DEFAULT '',
          allowed_sides TEXT NOT NULL,
          as_of TEXT NOT NULL,
          venue TEXT NOT NULL DEFAULT '',
          horizon_sessions INTEGER NOT NULL,
          forward_return REAL,
          verdict TEXT NOT NULL,
          flair_score REAL,
          evaluated_at TEXT NOT NULL,
          verdict_basis TEXT NOT NULL DEFAULT 'direction',
          candidate_scope_id TEXT,
          selector TEXT,
          opportunity REAL,
          bench_median_opportunity REAL,
          allocation_excess REAL,
          bench_n INTEGER,
          UNIQUE (mandate_id, symbol, as_of, horizon_sessions, verdict_basis)
        );
        CREATE TABLE universe_selection_metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    connection.executemany(
        """INSERT INTO process_events VALUES(
               ?, ?, 'cycle', '1', ?, ?, 'run', 'symbol', ?, 'instance_closed',
               '2026-08-21T10:03:00+00:00', '[]', 'completed', ?, ?, ?, '{}'
           )""",
        [
            (1, "event-1", "process-1", "process-attempt-1", "SAP.DE", "hold", "not_applied", "[]"),
            (2, "event-2", "process-1", "process-attempt-2", "IBM", "failed", "not_applied", "[]"),
        ],
    )
    connection.executemany(
        """INSERT INTO broker_fills VALUES(
               ?, 'SAP.DE', ?, 1, ?, ?, 0, 'EUR',
               'ibkr_europe_stock_tiered', 1, ?, 'process-1', ?
           )""",
        [
            (
                1,
                "BUY",
                100,
                "2026-08-21T10:00:00+00:00",
                "2026-08-21T10:00:00+00:00|0|SAP.DE",
                "process-attempt-1",
            ),
            (
                2,
                "SELL",
                110,
                "2026-08-22T10:00:00+00:00",
                "exit-sap",
                "process-attempt-exit",
            ),
        ],
    )
    selection_rows = [
        (
            "allocation",
            0.08,
            "BEAT_BENCH",
            0.7,
            "agent",
            0.08,
            0.03,
            0.05,
            12,
        ),
        (
            "direction",
            0.1,
            "WIN",
            0.6,
            "agent",
            None,
            None,
            None,
            None,
        ),
    ]
    connection.executemany(
        """INSERT INTO universe_selection_outcomes(
               mandate_id, symbol, family, role, allowed_sides, as_of, venue,
               horizon_sessions, forward_return, verdict, flair_score, evaluated_at,
               verdict_basis, candidate_scope_id, selector, opportunity,
               bench_median_opportunity, allocation_excess, bench_n
           ) VALUES(
               'mandate-sap', 'SAP.DE', 'europe_equity', 'candidate', '["long"]',
               '2026-08-21T09:30:00+00:00', 'EU', 5, ?, ?, ?,
               '2026-08-22T10:00:00+00:00', ?, 'scope-sap', ?, ?, ?, ?, ?
           )""",
        [
            (
                forward_return,
                verdict,
                flair_score,
                basis,
                selector,
                opportunity,
                bench_median,
                excess,
                bench_n,
            )
            for basis, forward_return, verdict, flair_score, selector, opportunity, bench_median, excess, bench_n in selection_rows
        ],
    )
    connection.execute(
        """INSERT INTO universe_selection_metadata VALUES(
               'selection_semantics_version', 'bench_v3', '2026-08-22T10:00:00+00:00'
           )"""
    )
    connection.commit()
    connection.close()


def _create_learnings_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE notes (
               decision_id TEXT PRIMARY KEY, ts TEXT, symbol TEXT, action TEXT,
               intent TEXT, executed INTEGER, reason TEXT, verdict TEXT,
               forward_return REAL, outcome_score REAL, q_value REAL,
               q_updates INTEGER, outcome_semantics_version INTEGER,
               evaluation_basis TEXT, horizon_used TEXT, evaluated_at TEXT,
               source_cycle_id TEXT
           )"""
    )
    connection.execute(
        """INSERT INTO notes VALUES(
               '2026-08-21T10:00:00+00:00|0|SAP.DE',
               '2026-08-21T10:00:00+00:00', 'SAP.DE', 'BUY', 'OPEN_LONG', 1,
               'executed', 'WIN', 0.1, 0.2, NULL, 0, 3,
               'realized', NULL, '2026-08-22T10:00:00+00:00', 'SAP.DE:1'
           )"""
    )
    connection.commit()
    connection.close()


def test_load_learning_outcomes_keeps_legacy_provenance_explicitly_missing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-learnings.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE notes (
               decision_id TEXT PRIMARY KEY, ts TEXT, symbol TEXT, action TEXT,
               intent TEXT, executed INTEGER, reason TEXT, verdict TEXT,
               forward_return REAL, outcome_score REAL, q_value REAL,
               q_updates INTEGER, outcome_semantics_version INTEGER
           )"""
    )
    connection.execute(
        """INSERT INTO notes VALUES(
               'legacy-decision', '2026-08-01T00:00:00+00:00', 'SPY', 'HOLD',
               'HOLD', 0, 'wait', 'WIN', 0.01, 0.1, 0, 0, 3
           )"""
    )
    connection.commit()
    connection.close()

    loaded = spike._load_learning_outcomes(path, ["legacy-decision"])

    assert {
        key: loaded["legacy-decision"][key]
        for key in (
            "evaluation_basis",
            "horizon_used",
            "evaluated_at",
            "source_cycle_id",
        )
    } == {
        "evaluation_basis": None,
        "horizon_used": None,
        "evaluated_at": None,
        "source_cycle_id": None,
    }


def _mandate_ref() -> dict:
    return {
        "mandate_id": "mandate-sap",
        "candidate_scope_id": "scope-sap",
        "venue": "EU",
        "agent_run_id": "universe-run-sap",
        "as_of": "2026-08-21T09:30:00+00:00",
        "status": "active",
    }


def _universe_context() -> dict:
    return {
        "mode": "active",
        "authority": "universe_mandate",
        "mandate_ref": _mandate_ref(),
        "symbol_mandate": {
            "why_selected": "relative strength with a clean catalyst",
            "directional_view": "bullish",
            "allowed_sides": ["long"],
            "posture": "engage",
            "role": "candidate",
            "confidence": 0.72,
        },
    }


def _create_universe_sources(state_dir: Path) -> None:
    symbol_mandate = _universe_context()["symbol_mandate"]
    _write_jsonl(
        state_dir / "universe_mandates" / "history.jsonl",
        [
            {
                "mandate_id": "mandate-sap",
                "candidate_scope_id": "scope-sap",
                "venue": "EU",
                "agent_run_id": None,
                "as_of": "2026-08-21T09:00:00+00:00",
                "status": "prepared",
                "symbols": {},
            },
            {
                **_mandate_ref(),
                "valid_until": "2026-08-22T09:30:00+00:00",
                "symbols": {"SAP.DE": symbol_mandate},
                "family_postures": {"europe_equity": "constructive"},
                "portfolio_posture": "selective_risk_on",
            },
        ],
    )
    _write_jsonl(
        state_dir / "universe_runs" / "2026-08-21.jsonl",
        [
            {
                "agent_run_id": "universe-run-sap",
                "candidate_scope_id": "scope-sap",
                "venue": "EU",
                "as_of": "2026-08-21T09:25:00+00:00",
                "status": "success",
                "agent_provider": "xai",
                "agent_model": "grok-4.6",
                "request_payload_hash": "sha256:fixture",
                "request_snapshot": {
                    "candidate_scope_id": "scope-sap",
                    "selection_feedback": {
                        "status": "observed",
                        "families": [{"family": "europe_equity", "verdict": "BEAT_BENCH"}],
                    },
                },
                "prompt_observation": {
                    "status": "actual_standard_tool_loop_projection",
                    "contract_version": "universe_prompt_v1",
                    "payload_hash": "sha256:prompt-fixture",
                    "payload": {"candidate_scope_id": "scope-sap"},
                },
                "tool_trace": {
                    "agent_run_id": "universe-run-sap",
                    "tool_rounds": 1,
                    "tool_calls": [{"id": "c1", "tool": "get_company_briefs", "outcome": "ok"}],
                    "tool_results": [{"id": "c1", "tool": "get_company_briefs", "ok": True}],
                },
                "input_signature": "fixture-signature",
                "selected_hotlist": ["SAP.DE"],
                "market_context": {"regime": "risk_on"},
                "symbol_rationales": {"SAP.DE": "relative strength"},
                "symbol_mandates": {"SAP.DE": symbol_mandate},
                "mandate_prepared_ref": {
                    "mandate_id": "mandate-sap",
                    "candidate_scope_id": "scope-sap",
                    "venue": "EU",
                    "as_of": "2026-08-21T09:00:00+00:00",
                    "status": "prepared",
                },
            }
        ],
    )
    _write_jsonl(
        state_dir / "candidate_scopes" / "2026-08-21.jsonl",
        [
            {
                "candidate_scope_id": "scope-sap",
                "venue": "EU",
                "as_of": "2026-08-21T09:00:00+00:00",
                "scope_phase": "daily",
                "candidates": ["SAP.DE", "ADS.DE"],
                "default_hotlist": ["SAP.DE"],
                "hotlist": ["SAP.DE"],
                "scores": {"SAP.DE": 0.9},
                "stale": False,
            }
        ],
    )


def test_parse_grok_session_separe_cli_et_resultat_metier(tmp_path: Path) -> None:
    cycle_id = "2026-08-21T10:00:00+00:00"
    shared = _shared(cycle_id, "SAP.DE")
    session_dir = _create_session(
        tmp_path / "sessions",
        session_id="session-1",
        cycle_id=cycle_id,
        symbol="SAP.DE",
        created_at="2026-08-21T10:00:01",
        shared=shared,
    )
    event_rows = [json.loads(line) for line in (session_dir / "events.jsonl").read_text().splitlines()]
    _write_jsonl(
        session_dir / "events.jsonl",
        event_rows
        + [
            {
                "ts": "2026-08-21T10:00:02Z",
                "type": "turn_started",
                "turn_number": 2,
            }
        ],
    )
    chat_rows = [json.loads(line) for line in (session_dir / "chat_history.jsonl").read_text().splitlines()]
    domain_turn = next(row for row in chat_rows if row.get("type") == "user" and row.get("prompt_index") == 1)
    marker = "# Nouveaux résultats (JSON)\n"
    prefix, raw_results = domain_turn["content"][0]["text"].split(marker, 1)
    domain_results = json.loads(raw_results)
    domain_results[0]["tool_results"].append(
        {
            "id": "validation:propose_indicator_watch:SAP.DE:1",
            "tool": "propose_indicator_watch",
            "ok": False,
            "result": None,
            "error": [{"reason": "unknown_indicator"}],
        }
    )
    domain_turn["content"][0]["text"] = prefix + marker + json.dumps(domain_results)
    _write_jsonl(
        session_dir / "chat_history.jsonl",
        chat_rows
        + [
            {
                "type": "user",
                "prompt_index": 2,
                "synthetic_reason": "background_task_completed",
                "content": [{"type": "text", "text": "synthetic completion"}],
            }
        ],
    )

    parsed = spike.parse_grok_session(session_dir)

    assert parsed["prompt"]["cycle_id"] == cycle_id
    assert parsed["prompt"]["symbol"] == "SAP.DE"
    assert parsed["native_steps"][0]["tool_name"] == "read_file"
    assert parsed["assistant_model_counts"] == {"grok-4.6-build": 3}
    assert parsed["native_steps"][0]["arguments"]["category"] == "prompt_file_read"
    assert parsed["native_steps"][0]["result"]["status"] == "error"
    assert len(parsed["domain_steps"]) == 2
    domain = next(step for step in parsed["domain_steps"] if step["tool"] == "evaluate_trade_plan")
    assert domain["join_status"] == "matched_by_local_call_id"
    assert domain["response"]["transport_ok"] is True
    assert domain["response"]["semantic_valid"] is False
    assert domain["response"]["rejection_codes"] == [
        "risk_pct_out_of_range",
        "invalid_exit_plan",
    ]
    validation = next(step for step in parsed["domain_steps"] if step["tool"] == "propose_indicator_watch")
    assert validation["join_status"] == "response_without_request"
    assert validation["args"] is None
    assert validation["response"]["transport_ok"] is False
    assert [turn["output_kind"] for turn in parsed["turns"]] == [
        "domain_tool_request",
        "decision",
    ]
    assert parsed["turns"][0]["ended_at"] == "2026-08-21T10:00:01Z"
    assert parsed["turns"][0]["turn_outcome"] == "completed"
    assert parsed["discarded_non_authoritative_turn_count"] == 1


def test_reconstruct_joint_retries_dead_effects_et_outcome(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sessions_dir = tmp_path / "sessions"
    cycle_done = "2026-08-21T10:00:00+00:00"
    cycle_dead = "2026-08-21T10:05:00+00:00"
    shared_done = _shared(cycle_done, "SAP.DE")
    shared_dead = _shared(cycle_dead, "IBM")
    done_facts = {
        "session": {"open": True},
        "feature": float("nan"),
        "universe_mandate": _universe_context(),
    }
    payload_done = {
        "cycle_id": cycle_done,
        "symbol": "SAP.DE",
        "shared_context": shared_done,
        "per_symbol_facts": done_facts,
        "process": {
            "process_instance_id": "process-1",
            "attempt_id": "process-attempt-1",
            "runtime_run_id": "run",
        },
    }
    payload_dead = {
        "cycle_id": cycle_dead,
        "symbol": "IBM",
        "shared_context": shared_dead,
        "per_symbol_facts": {"session": {"open": True}},
        "process": {
            "process_instance_id": "process-1",
            "attempt_id": "process-attempt-2",
            "runtime_run_id": "run",
        },
    }
    _create_task_db(
        state_dir / "task_ledger.db",
        [
            {
                "id": 1,
                "cycle_id": cycle_done,
                "symbol": "SAP.DE",
                "payload": payload_done,
                "status": "done",
                "attempts": 2,
                "result": {"model_calls": 2, "process": payload_done["process"]},
                "error": "llm_error:parse_error:corrupt_element",
                "created_at": "2026-08-21T10:00:00",
                "updated_at": "2026-08-21T10:02:00",
            },
            {
                "id": 2,
                "cycle_id": cycle_dead,
                "symbol": "IBM",
                "payload": payload_dead,
                "status": "dead",
                "attempts": 1,
                "result": None,
                "error": "llm_error:parse_error:missing_symbol",
                "created_at": "2026-08-21T10:05:00",
                "updated_at": "2026-08-21T10:06:00",
            },
        ],
    )
    _create_session(
        sessions_dir,
        session_id="retry-1",
        cycle_id=cycle_done,
        symbol="SAP.DE",
        created_at="2026-08-21T10:00:01",
        shared=shared_done,
        symbol_facts={**payload_done["per_symbol_facts"], "session": {"open": False}},
        turn_count=1,
    )
    _create_session(
        sessions_dir,
        session_id="success-2",
        cycle_id=cycle_done,
        symbol="SAP.DE",
        created_at="2026-08-21T10:01:01",
        shared=shared_done,
        symbol_facts=payload_done["per_symbol_facts"],
        turn_count=2,
    )
    _create_session(
        sessions_dir,
        session_id="dead-1",
        cycle_id=cycle_dead,
        symbol="IBM",
        created_at="2026-08-21T10:05:01",
        shared=shared_dead,
        symbol_facts={"session": {"open": False}},
        turn_count=1,
    )
    _write_jsonl(
        state_dir / "decisions.jsonl",
        [
            {
                "decision_id": cycle_done + "|0|SAP.DE",
                "cycle_ts": cycle_done,
                "sequence": 0,
                "symbol": "SAP.DE",
                "decision_source": "llm",
                "model_called": True,
                "action": "BUY",
                "intent": "OPEN_LONG",
                "confidence": 0.72,
                "decision_reason_code": "CATALYST_CONFIRMED",
                "executed": True,
                "reason": "executed",
                "rationale": "Catalyst and relative strength align.",
                "opportunity_side": "long",
                "mandate_ref": _mandate_ref(),
                "decision": {
                    "tool_rounds": 0,
                    "tool_calls": [],
                    "process_instance_id": "process-1",
                    "thesis": {
                        "claim": "earnings catalyst should extend relative strength",
                        "invalidation": "break below support",
                    },
                },
                "process": payload_done["process"],
            }
        ],
    )
    _create_universe_sources(state_dir)
    _create_casys_db(state_dir / "casys.db")
    _create_learnings_db(state_dir / "learnings.db")
    daemon_log = state_dir / "daemon_console.log"
    daemon_log.write_text(
        "2026-08-21 18:00:10 INFO [acpx_call] session=1:0 symbol=SAP.DE "
        "pid=1 provider=acpx timeout_s=240 dur_s=10 outcome=ok\n"
        "2026-08-21 18:00:11 WARNING [queue.ledger] retry kind=decide symbol=SAP.DE "
        "id=1 attempt=1/3 delay_s=1 error=llm_error:parse_error:corrupt_element\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    config = spike.SpikeConfig(
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        daemon_log=daemon_log,
        output_dir=output_dir,
    )

    summary, datasets = spike.reconstruct(config)

    assert summary["coverage"]["tasks"] == 2
    assert summary["coverage"]["expected_attempts"] == 3
    assert summary["coverage"]["matched_raw_sessions"] == 3
    assert summary["coverage"]["decisions_joined"] == 1
    assert summary["coverage"]["done_decision_join_rate"] == 1.0
    assert summary["coverage"]["decision_process_identity_exact"] == 1
    assert summary["coverage"]["processes_joined"] == 2
    assert summary["coverage"]["tasks_with_prompt_shared_context_match"] == 2
    assert summary["coverage"]["tasks_with_prompt_symbol_facts_match"] == 0
    assert summary["coverage"]["tasks_with_consistent_prompt_observation"] == 1
    assert summary["coverage"]["decisions_with_fills"] == 1
    assert summary["coverage"]["decisions_with_learning_outcome"] == 1
    assert summary["coverage"]["task_payloads_with_nonfinite_replaced_on_export"] == 1
    assert summary["grok_mechanics"]["raw_acp_turns"] == 4
    assert summary["grok_mechanics"]["successful_attempt_model_calls"] == 2
    assert summary["grok_mechanics"]["failed_attempt_turns_not_in_task_result"] == 2
    assert summary["observed_patterns"]["cohort"]["task_attempt_count_distribution"] == {
        "1": 1,
        "2": 1,
    }
    assert summary["observed_patterns"]["native_cli"]["sessions_with_event_error"] == 3
    assert summary["observed_patterns"]["evaluate_trade_plan"]["calls"] == 3
    assert summary["cross_loop"]["episodes"] == 2
    assert summary["cross_loop"]["primary_target"] == "target.trader_flair"
    assert summary["cross_loop"]["hypotheses"] == {
        "structured_thesis_present": 1,
        "rationale_present": 1,
        "evaluate_trade_plan_present": 2,
    }
    assert summary["cross_loop"]["targets"]["trader_flair"]["maturity"] == {
        "mature": 1,
        "missing_trace": 1,
    }
    assert summary["cross_loop"]["targets"]["execution"] == {
        "with_immediate_fill": 1,
        "with_realised_cycle": 1,
        "fee_complete": 1,
    }
    assert summary["inputs"]["universe_selection_outcomes"]["semantics_trusted"] is True
    done = datasets["trajectories"][0]
    dead = datasets["trajectories"][1]
    assert [attempt["queue_outcome"] for attempt in done["attempts"]] == ["retry", "success"]
    assert dead["decision"] is None
    assert dead["join_quality"]["decision"] == "missing"
    assert dead["join_quality"]["decision_process_identity"] == ("not_applicable_without_decision")
    assert [row["attempt_id"] for row in done["effects"]["process_events"]] == ["process-attempt-1"]
    assert [row["attempt_id"] for row in dead["effects"]["process_events"]] == ["process-attempt-2"]
    assert done["join_quality"]["prompt_symbol_facts_all_match"] is False
    assert dead["join_quality"]["prompt_symbol_facts_all_match"] is False
    assert done["model_observation"]["symbol_block"]["session"]["open"] is True
    assert done["model_observation"]["consistent_across_attempts"] is False
    assert dead["model_observation"]["symbol_block"]["session"]["open"] is False
    assert len(datasets["cross_loop_episodes"]) == 2
    episode = done["cross_loop"]
    assert episode == datasets["cross_loop_episodes"][0]
    assert episode["universe_context"]["mandate_ref"] == _mandate_ref()
    assert episode["lineage"]["mandate_activation"]["status"] == "exact"
    assert episode["lineage"]["universe_run"]["status"] == "exact"
    assert episode["lineage"]["candidate_scope"]["status"] == "exact"
    assert episode["lineage"]["universe_selection_flair"]["status"] == "exact"
    universe_run = episode["universe_context"]["universe_run"]
    assert universe_run["point_in_time_selection_feedback"]["status"] == "observed"
    assert universe_run["point_in_time_selection_feedback"]["payload"]["families"][0][
        "verdict"
    ] == "BEAT_BENCH"
    assert universe_run["prompt_observation"]["contract_version"] == "universe_prompt_v1"
    assert universe_run["tool_trace"]["tool_results"][0]["ok"] is True
    assert episode["hypotheses"]["universe"]["why_selected"] == ("relative strength with a clean catalyst")
    assert episode["hypotheses"]["universe"]["allowed_sides"] == ["long"]
    assert episode["hypotheses"]["trader"]["structured_thesis"]["claim"].startswith("earnings catalyst")
    assert episode["hypotheses"]["trader"]["rationale"] == ("Catalyst and relative strength align.")
    assert len(episode["hypotheses"]["trader"]["evaluate_trade_plan_results"]) == 2
    trader_flair = episode["target"]["trader_flair"]
    assert trader_flair["maturity"] == "mature"
    assert trader_flair["basis"] == "realised_flat_to_flat_fee_complete"
    assert trader_flair["verdict"] == "WIN"
    assert trader_flair["reward"] == 1.0
    assert trader_flair["forward_return"] == pytest.approx(0.1)
    assert trader_flair["outcome_score"] == pytest.approx(0.2)
    assert trader_flair["evaluation_basis"] == "realized"
    assert trader_flair["evaluated_at"] == "2026-08-22T10:00:00+00:00"
    assert trader_flair["source_cycle_id"] == "SAP.DE:1"
    execution = episode["target"]["execution"]
    assert execution["fee_complete"] is True
    assert execution["position_cycles"][0]["pnl"] == pytest.approx(10.0)
    assert execution["position_cycles"][0]["net_return"] == pytest.approx(0.1)
    universe_flair = episode["target"]["universe_selection_flair"]
    assert universe_flair["primary_basis"] == "allocation"
    assert universe_flair["allocation"]["status"] == "evaluated"
    assert universe_flair["allocation"]["outcomes"][0]["verdict"] == "BEAT_BENCH"
    assert universe_flair["direction"]["status"] == "evaluated"
    assert episode["target"]["memrl"] == {"q_value": None, "q_updates": 0}
    assert episode["evaluation_contract"]["views"]["action_ex_ante"]["excluded"] == [
        "hypotheses.trader",
        "trader_workflow",
        "decision",
        "target",
    ]
    action_view = episode["feature_views"]["action_ex_ante"]
    assert action_view["market_state"]["source"]["session_id"] == "success-2"
    assert action_view["universe"]["observed_context"]["mandate_ref"] == _mandate_ref()
    assert "refs" not in action_view["universe"]
    assert "experiment_id" not in action_view["configuration"]
    assert "label_available" not in action_view
    assert "selection" not in action_view["market_state"]["source"]
    assert "decision_row" not in json.dumps(action_view)
    assert episode["training_eligibility"]["action_ex_ante"]["status"] == "eligible"
    assert dead["cross_loop"]["lineage"]["mandate_activation"]["status"] == "absent"
    assert dead["cross_loop"]["target"]["trader_flair"]["maturity"] == "missing_trace"
    assert dead["cross_loop"]["training_eligibility"]["action_ex_ante"]["status"] == ("missing_trace")

    spike.write_datasets(output_dir, summary, datasets)
    exported = [json.loads(line) for line in (output_dir / "trajectories.jsonl").read_text().splitlines()]
    assert exported[0]["state_before"]["per_symbol_facts"]["feature"] is None
    exported_episodes = [
        json.loads(line) for line in (output_dir / "cross_loop_episodes.jsonl").read_text().splitlines()
    ]
    assert exported_episodes[0]["target"]["trader_flair"]["verdict"] == "WIN"
    assert json.loads((output_dir / "summary.json").read_text())["status"] == "ok"
    with pytest.raises(ValueError, match="not empty"):
        spike.write_datasets(output_dir, summary, datasets)


def test_main_missing_inputs_is_read_only_and_invalid_date_is_code_2(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "missing-state"
    sessions_dir = tmp_path / "missing-sessions"

    assert (
        spike.main(
            [
                "--state-dir",
                str(state_dir),
                "--sessions-dir",
                str(sessions_dir),
                "--daemon-log",
                str(state_dir / "daemon.log"),
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "insufficient_data"
    assert not state_dir.exists()
    assert not sessions_dir.exists()

    with pytest.raises(SystemExit) as raised:
        spike.main(["--since", "not-a-date"])
    assert raised.value.code == 2


def test_sqlite_immutable_refuse_un_wal_non_vide(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE sample (value INTEGER)")
    connection.commit()
    connection.close()
    Path(str(database) + "-wal").write_bytes(b"uncheckpointed")

    with pytest.raises(RuntimeError, match="non-empty WAL"):
        spike._sqlite_ro(database)


def test_queue_attempt_outcome_ne_depend_pas_du_log_daemon() -> None:
    assert spike._queue_attempt_outcome(task_status="done", expected_attempts=2, attempt_index=1) == "retry"
    assert spike._queue_attempt_outcome(task_status="done", expected_attempts=2, attempt_index=2) == "success"
    assert spike._queue_attempt_outcome(task_status="dead", expected_attempts=3, attempt_index=3) == "dead"


def test_trader_flair_pending_et_cycle_realise_fee_complete_obligatoire() -> None:
    empty_execution = {
        "has_realised_cycle": False,
        "fee_complete": False,
    }
    authentic = {
        "decision_source": "llm",
        "model_called": True,
        "rationale": "An exploitable rationale for the fixture.",
    }
    pending = spike._trader_flair_target(
        decision_row={
            **authentic,
            "decision_id": "decision-hold",
            "action": "HOLD",
            "intent": "HOLD",
            "opportunity_side": "long",
            "executed": False,
        },
        learning_outcome={
            "verdict": None,
            "forward_return": None,
            "outcome_score": None,
            "outcome_semantics_version": None,
        },
        execution=empty_execution,
    )
    assert pending["maturity"] == "pending"
    assert pending["basis"] == "counterfactual_market_1d_or_fallback_4h"
    assert pending["verdict"] is None

    inconsistent_open = spike._trader_flair_target(
        decision_row={
            **authentic,
            "decision_id": "decision-open",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
        },
        learning_outcome={
            "verdict": "WIN",
            "forward_return": 0.1,
            "outcome_score": 0.4,
            "outcome_semantics_version": 3,
        },
        execution=empty_execution,
    )
    assert inconsistent_open["maturity"] == "inconsistent_trace"
    assert inconsistent_open["basis"] == "realised_flat_to_flat_fee_complete"
    assert inconsistent_open["basis_status"] == "pending_or_fee_incomplete"
    assert inconsistent_open["verdict"] is None
    assert inconsistent_open["forward_return"] is None
    assert inconsistent_open["outcome_score"] is None

    canonical_execution = {
        "has_realised_cycle": True,
        "fee_complete": True,
        "position_cycles": [
            {
                "position_cycle_id": "SAP.DE:1",
                "outcome_eligible": True,
                "net_return": 0.1,
            }
        ],
    }
    verified_open = spike._trader_flair_target(
        decision_row={
            **authentic,
            "decision_id": "decision-verified-open",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
        },
        learning_outcome={
            "verdict": "WIN",
            "forward_return": 0.1,
            "outcome_score": 0.4,
            "outcome_semantics_version": 3,
        },
        execution=canonical_execution,
    )
    assert verified_open["realised_consistency"]["status"] == "verified"
    assert verified_open["trainable_label"] is True

    mismatched_open = spike._trader_flair_target(
        decision_row={
            **authentic,
            "decision_id": "decision-mismatched-open",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
        },
        learning_outcome={
            "verdict": "LOSS",
            "forward_return": -0.1,
            "outcome_score": -0.4,
            "outcome_semantics_version": 3,
        },
        execution=canonical_execution,
    )
    assert mismatched_open["maturity"] == "inconsistent_trace"
    assert mismatched_open["realised_consistency"]["status"] == "inconsistent"
    assert mismatched_open["verdict"] is None
    assert mismatched_open["trainable_label"] is False

    synthetic_open = spike._trader_flair_target(
        decision_row={
            "decision_id": "synth:infra-open",
            "decision_source": "infra",
            "model_called": False,
            "rationale": "synthetic infra row",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
        },
        learning_outcome={
            "verdict": "WIN",
            "forward_return": 0.1,
            "outcome_score": 0.4,
            "outcome_semantics_version": 3,
        },
        execution=empty_execution,
    )
    assert synthetic_open["maturity"] == "not_applicable"
    assert synthetic_open["basis"] == "counterfactual_market_1d_or_fallback_4h"
    assert synthetic_open["verdict"] is None
    assert synthetic_open["trainable_label"] is False

    orphan_note = spike._trader_flair_target(
        decision_row=None,
        learning_outcome={
            "verdict": "WIN",
            "forward_return": 0.1,
            "outcome_score": 0.4,
            "outcome_semantics_version": 3,
        },
        execution=empty_execution,
    )
    assert orphan_note["eligibility"] == {
        "status": "missing_trace",
        "reason": "decision_missing",
    }
    assert orphan_note["maturity"] == "missing_trace"
    assert orphan_note["basis"] == "not_applicable"
    assert orphan_note["trainable_label"] is False

    unknown = spike._trader_flair_target(
        decision_row={
            **authentic,
            "decision_id": "decision-unknown",
            "action": "CLOSE",
            "intent": "CLOSE",
            "executed": False,
        },
        learning_outcome={
            "verdict": "UNKNOWN",
            "forward_return": None,
            "outcome_score": 0.0,
            "outcome_semantics_version": 3,
        },
        execution=empty_execution,
    )
    assert unknown["maturity"] == "evaluated_unknown"
    assert unknown["trainable_label"] is False
    assert unknown["reward"] is None

    current_without_flair_score = spike._trader_flair_target(
        decision_row={
            **authentic,
            "decision_id": "decision-current",
            "action": "CLOSE",
            "intent": "CLOSE",
            "executed": False,
        },
        learning_outcome={
            "verdict": "WIN",
            "forward_return": 0.1,
            "outcome_score": None,
            "outcome_semantics_version": 3,
        },
        execution=empty_execution,
    )
    assert current_without_flair_score["trainable_label"] is True
    assert current_without_flair_score["reward"] == 1.0
    assert current_without_flair_score["flair_score_status"] == "pending"

    stale = spike._trader_flair_target(
        decision_row={
            **authentic,
            "decision_id": "decision-stale",
            "action": "SELL",
            "intent": "CLOSE",
            "executed": False,
        },
        learning_outcome={
            "verdict": "WIN",
            "forward_return": 0.1,
            "outcome_score": 0.4,
            "outcome_semantics_version": 2,
        },
        execution=empty_execution,
    )
    assert stale["maturity"] == "stale_semantics"
    assert stale["trainable_label"] is False
    assert stale["verdict"] is None

    undirected_hold = spike._trader_flair_target(
        decision_row={
            "decision_id": "decision-undirected-hold",
            "decision_source": "llm",
            "model_called": True,
            "rationale": "No edge.",
            "action": "HOLD",
            "intent": "HOLD",
            "executed": False,
        },
        learning_outcome=None,
        execution=empty_execution,
    )
    assert undirected_hold["eligibility"] == {
        "status": "not_applicable",
        "reason": "undirected_hold_not_selected_for_learning",
    }
    assert undirected_hold["maturity"] == "not_applicable"

    missing_note = spike._trader_flair_target(
        decision_row={
            "decision_id": "decision-missing-note",
            "decision_source": "llm",
            "model_called": True,
            "rationale": "Directional entry thesis.",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
        },
        learning_outcome=None,
        execution=empty_execution,
    )
    assert missing_note["eligibility"] == {
        "status": "missing_trace",
        "reason": "eligible_learning_note_missing",
    }
    assert missing_note["maturity"] == "missing_trace"


def test_mandate_duplicate_est_absent_et_ref_partiel_unique_est_reconstruit() -> None:
    active = {
        **_mandate_ref(),
        "symbols": {"SAP.DE": _universe_context()["symbol_mandate"]},
    }
    quality = {"semantics_trusted": True, "semantics_version": "bench_v3"}
    duplicated = spike._resolve_universe_lineage(
        symbol="SAP.DE",
        cycle_id="2026-08-21T10:00:00+00:00",
        task_facts={"universe_mandate": _universe_context()},
        model_symbol_block={"universe_mandate": _universe_context()},
        decision_row={"mandate_ref": _mandate_ref()},
        sources={
            "mandates_by_id": {"mandate-sap": [dict(active), dict(active)]},
            "runs_by_id": {
                "universe-run-sap": [
                    {"agent_run_id": "universe-run-sap", "as_of": active["as_of"]},
                    {"agent_run_id": "universe-run-sap", "as_of": active["as_of"]},
                ]
            },
            "runs_by_mandate": {},
            "scopes_by_id": {
                "scope-sap": [
                    {"candidate_scope_id": "scope-sap", "as_of": active["as_of"]},
                    {"candidate_scope_id": "scope-sap", "as_of": active["as_of"]},
                ]
            },
        },
        selection_exact={},
        selection_broad={},
        selection_source_quality=quality,
    )
    assert duplicated["mandate_snapshot"] is None
    assert duplicated["universe_run"] is None
    assert duplicated["candidate_scope"] is None
    assert duplicated["lineage"]["mandate_activation"]["status"] == "absent"
    assert duplicated["lineage"]["universe_run"]["status"] == "absent"
    assert duplicated["lineage"]["candidate_scope"]["status"] == "absent"

    partial_context = {
        "mandate_ref": {"mandate_id": "mandate-sap"},
        "symbol_mandate": _universe_context()["symbol_mandate"],
    }
    reconstructed = spike._resolve_universe_lineage(
        symbol="SAP.DE",
        cycle_id="2026-08-21T10:00:00+00:00",
        task_facts={"universe_mandate": partial_context},
        model_symbol_block={"universe_mandate": partial_context},
        decision_row=None,
        sources={
            "mandates_by_id": {"mandate-sap": [active]},
            "runs_by_id": {},
            "runs_by_mandate": {},
            "scopes_by_id": {},
        },
        selection_exact={},
        selection_broad={},
        selection_source_quality=quality,
    )
    assert reconstructed["lineage"]["mandate_activation"]["status"] == "reconstructed"

    ambiguous_selection = spike._resolve_universe_lineage(
        symbol="SAP.DE",
        cycle_id="2026-08-21T10:00:00+00:00",
        task_facts={"universe_mandate": partial_context},
        model_symbol_block={"universe_mandate": partial_context},
        decision_row=None,
        sources={
            "mandates_by_id": {"mandate-sap": [active]},
            "runs_by_id": {},
            "runs_by_mandate": {},
            "scopes_by_id": {},
        },
        selection_exact={},
        selection_broad={
            ("mandate-sap", "SAP.DE"): [
                {"as_of": "2026-08-21T09:20:00+00:00"},
                {"as_of": "2026-08-21T09:25:00+00:00"},
            ]
        },
        selection_source_quality=quality,
    )
    assert ambiguous_selection["selection_outcomes"] == []
    assert ambiguous_selection["lineage"]["universe_selection_flair"] == {
        "status": "absent",
        "method": "mandate_id+symbol",
        "reason": "ambiguous_multiple_activation_as_of",
        "matches": 2,
    }

    invalid_ref = {**_mandate_ref(), "as_of": "not-a-timestamp"}
    invalid_context = {
        "mandate_ref": invalid_ref,
        "symbol_mandate": _universe_context()["symbol_mandate"],
    }
    invalid = spike._resolve_universe_lineage(
        symbol="SAP.DE",
        cycle_id="2026-08-21T10:00:00+00:00",
        task_facts={"universe_mandate": invalid_context},
        model_symbol_block={"universe_mandate": invalid_context},
        decision_row=None,
        sources={
            "mandates_by_id": {"mandate-sap": [{**active, "as_of": "not-a-timestamp"}]},
            "runs_by_id": {},
            "runs_by_mandate": {},
            "scopes_by_id": {},
        },
        selection_exact={},
        selection_broad={},
        selection_source_quality=quality,
    )
    assert invalid["lineage"]["mandate_activation"]["status"] == "absent"

    future_ref = {**_mandate_ref(), "as_of": "2026-08-22T09:30:00+00:00"}
    future_context = {
        "mandate_ref": future_ref,
        "symbol_mandate": _universe_context()["symbol_mandate"],
    }
    future = spike._resolve_universe_lineage(
        symbol="SAP.DE",
        cycle_id="2026-08-21T10:00:00+00:00",
        task_facts={"universe_mandate": future_context},
        model_symbol_block={"universe_mandate": future_context},
        decision_row=None,
        sources={
            "mandates_by_id": {"mandate-sap": [{**active, **future_ref}]},
            "runs_by_id": {},
            "runs_by_mandate": {},
            "scopes_by_id": {},
        },
        selection_exact={},
        selection_broad={},
        selection_source_quality=quality,
    )
    assert future["lineage"]["mandate_activation"]["status"] == "absent"


def test_execution_exige_economie_complete_et_garde_attribution_partagee_exacte(
    tmp_path: Path,
) -> None:
    shared_cycle = {
        "position_cycle_id": "SAP.DE:1",
        "entry_decision_ids": ["entry-a", "entry-b"],
        "entry_notional_usd": 100.0,
        "pnl": 10.0,
        "commission_quality": {"status": "available"},
    }
    shared = spike._execution_target(
        decision_id="entry-a",
        immediate_fills=[],
        cycles_by_decision={"entry-a": [shared_cycle]},
    )
    assert shared["lineage"]["status"] == "exact"
    assert shared["attribution_grain"] == "shared_position_cycle"
    assert shared["fee_complete"] is True
    assert shared["position_cycles"][0]["net_return"] == pytest.approx(0.1)

    zero_notional = spike._execution_target(
        decision_id="entry-a",
        immediate_fills=[],
        cycles_by_decision={"entry-a": [{**shared_cycle, "entry_notional_usd": 0.0}]},
    )
    assert zero_notional["fee_complete"] is False
    assert zero_notional["position_cycles"][0]["outcome_eligible"] is False
    assert zero_notional["position_cycles"][0]["net_return"] is None

    unknown_fee = spike._execution_target(
        decision_id="entry-a",
        immediate_fills=[],
        cycles_by_decision={
            "entry-a": [
                {
                    **shared_cycle,
                    "commission_quality": {"status": "unavailable"},
                }
            ]
        },
    )
    assert unknown_fee["fee_complete"] is False

    partial_db = tmp_path / "partial.db"
    connection = sqlite3.connect(partial_db)
    connection.execute(
        """CREATE TABLE broker_fills(
               seq INTEGER PRIMARY KEY, symbol TEXT, side TEXT, quantity REAL,
               price REAL, ts TEXT, commission REAL, commission_currency TEXT,
               commission_model TEXT, fx_rate REAL, decision_id TEXT
           )"""
    )
    connection.executemany(
        """INSERT INTO broker_fills VALUES(
               ?, 'SAP.DE', ?, ?, ?, ?, 0, 'EUR',
               'ibkr_europe_stock_tiered', 1, ?
           )""",
        [
            (1, "BUY", 2, 100, "2026-08-21T10:00:00+00:00", "entry-partial"),
            (2, "SELL", 1, 110, "2026-08-22T10:00:00+00:00", "exit-partial"),
        ],
    )
    connection.commit()
    connection.close()
    cycles, quality = spike._load_execution_cycles(partial_db)
    assert cycles == {}
    assert quality["cycles"] == 0


def test_universe_flair_isole_agent_5j_fallback_horizons_et_direction() -> None:
    def row(*, selector: str, basis: str, horizon: int, score: float | None) -> dict:
        return {
            "selector": selector,
            "verdict_basis": basis,
            "horizon_sessions": horizon,
            "verdict": "gagnant",
            "flair_score": score,
        }

    target = spike._universe_flair_target(
        selection_rows=[
            row(selector="agent", basis="allocation", horizon=5, score=0.4),
            row(selector="agent", basis="direction", horizon=5, score=None),
            row(selector="baseline_fallback", basis="allocation", horizon=5, score=0.2),
            row(selector="agent", basis="allocation", horizon=10, score=0.9),
        ],
        selection_quality={"semantics_trusted": True, "semantics_version": "bench_v3"},
        symbol_mandate={"allowed_sides": ["long", "short"]},
        lineage={"status": "exact", "method": "fixture"},
    )
    assert target["primary_cohort"] == {
        "selector": "agent",
        "horizon_sessions": 5,
        "verdict_basis": "allocation",
    }
    assert target["status"] == "evaluated"
    assert len(target["allocation"]["outcomes"]) == 1
    assert target["allocation"]["outcomes"][0]["selector"] == "agent"
    assert target["direction"]["status"] == "not_applicable"
    assert len(target["baseline_fallback"]["allocation"]["outcomes"]) == 1
    assert target["baseline_fallback"]["allocation"]["outcomes"][0]["selector"] == ("baseline_fallback")
    assert len(target["other_horizons"]) == 1
    assert target["other_horizons"][0]["horizon_sessions"] == 10

    directional = spike._universe_flair_target(
        selection_rows=[
            row(selector="agent", basis="allocation", horizon=5, score=0.4),
            row(selector="agent", basis="direction", horizon=5, score=None),
        ],
        selection_quality={"semantics_trusted": True, "semantics_version": "bench_v3"},
        symbol_mandate={"allowed_sides": ["long"]},
        lineage={"status": "exact", "method": "fixture"},
    )
    assert directional["direction"]["status"] == "verdict_evaluated_flair_pending"

    stale = spike._universe_flair_target(
        selection_rows=[row(selector="agent", basis="allocation", horizon=5, score=0.4)],
        selection_quality={"semantics_trusted": False, "semantics_version": "bench_v2"},
        symbol_mandate={"allowed_sides": ["long"]},
        lineage={"status": "exact", "method": "fixture"},
    )
    assert stale["status"] == "stale_or_unversioned"

    stale_without_rows = spike._universe_flair_target(
        selection_rows=[],
        selection_quality={"semantics_trusted": False, "semantics_version": "bench_v2"},
        symbol_mandate={"allowed_sides": ["long"]},
        lineage={"status": "absent", "method": "fixture"},
        mandate_status="active",
    )
    assert stale_without_rows["status"] == "stale_or_unversioned"

    fallback_only = spike._universe_flair_target(
        selection_rows=[
            row(
                selector="baseline_fallback",
                basis="allocation",
                horizon=5,
                score=0.2,
            )
        ],
        selection_quality={"semantics_trusted": True, "semantics_version": "bench_v3"},
        symbol_mandate={"allowed_sides": ["long"]},
        lineage={"status": "exact", "method": "fixture"},
        mandate_status="fallback",
    )
    assert fallback_only["status"] == "not_applicable"
    assert fallback_only["allocation"]["status"] == "not_applicable"
    assert fallback_only["baseline_fallback"]["allocation"]["status"] == "evaluated"
