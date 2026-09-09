"""Isolated tests for the global_rule_citations repair CLI."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import repair_learning_rule_citations as repair

SINCE = "2026-09-04T00:00:00+00:00"
UTC = timezone.utc

CITATION_DDL = """
CREATE TABLE global_rule_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    rule_ids TEXT NOT NULL,
    ts TEXT NOT NULL,
    verdict TEXT,
    reward REAL,
    forward_return REAL,
    evaluated_at TEXT,
    outcome_semantics_version INTEGER
);
"""

RULES_DDL = """
CREATE TABLE global_rules (
    rule_id TEXT PRIMARY KEY,
    q_value REAL NOT NULL DEFAULT 0,
    q_updates INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    retired_at TEXT,
    updated_at TEXT
);
"""

METADATA_DDL = """
CREATE TABLE learnings_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

NOTES_DDL = """
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    decision_id TEXT,
    q_value REAL NOT NULL DEFAULT 0,
    q_updates INTEGER NOT NULL DEFAULT 0
);
"""


def _sha256_line(line: str) -> str:
    return hashlib.sha256(line.rstrip("\r\n").encode("utf-8")).hexdigest()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return lines


def _write_gzip_jsonl(path: Path, rows: list[dict[str, Any]]) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))
    return lines


def _llm_row(
    decision_id: str,
    *,
    cycle_ts: str,
    rule_ids: list[str] | None = None,
    symbol: str = "SPY",
    **updates: object,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "decision_id": decision_id,
        "cycle_ts": cycle_ts,
        "symbol": symbol,
        "decision_source": "llm",
        "model_called": True,
        "llm_error": None,
        "dry_run": False,
        "applied_learning_ids": list(rule_ids) if rule_ids is not None else ["rule-fees"],
        "runtime": {"dry_run": False},
        "decision": {
            "decision_id": decision_id,
            "symbol": symbol,
            "applied_learning_ids": list(rule_ids) if rule_ids is not None else ["rule-fees"],
            "decision_source": "llm",
            "model_called": True,
        },
    }
    row.update(updates)
    return row


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _create_fixture_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(RULES_DDL + CITATION_DDL + METADATA_DDL + NOTES_DDL)
    conn.execute(
        "INSERT INTO learnings_metadata(key, value, updated_at) VALUES (?, ?, ?)",
        ("outcome_semantics_version", "3", "2026-09-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO notes(id, decision_id, q_value, q_updates) VALUES (1, 'note-1', 0.42, 7)"
    )


def _insert_rule(
    conn: sqlite3.Connection,
    rule_id: str,
    *,
    active: int = 1,
    q_value: float = 0.25,
    q_updates: int = 4,
    retired_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO global_rules(
            rule_id, q_value, q_updates, active, created_at, retired_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rule_id,
            q_value,
            q_updates,
            active,
            "2026-08-01T00:00:00+00:00",
            retired_at,
            "2026-08-01T00:00:00+00:00",
        ),
    )


def _insert_citation(
    conn: sqlite3.Connection,
    decision_id: str,
    rule_ids: list[str],
    ts: str,
    *,
    verdict: str | None = None,
    reward: float | None = None,
    forward_return: float | None = None,
    evaluated_at: str | None = None,
    outcome_semantics_version: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO global_rule_citations(
            decision_id, rule_ids, ts, verdict, reward, forward_return,
            evaluated_at, outcome_semantics_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            json.dumps(rule_ids),
            ts,
            verdict,
            reward,
            forward_return,
            evaluated_at,
            outcome_semantics_version,
        ),
    )


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()
        (self.state_dir / "archive").mkdir()
        self.db_path = self.state_dir / "learnings.db"
        self.ledger = self.state_dir / "decisions.jsonl"
        self.archive_dir = self.state_dir / "archive"
        self.output = tmp_path / "audit" / "repair.json"
        conn = _connect(self.db_path)
        _create_fixture_schema(conn)
        _insert_rule(conn, "rule-fees")
        _insert_rule(
            conn,
            "rule-retired",
            active=0,
            q_value=-0.3,
            q_updates=9,
            retired_at="2026-09-03T00:00:00+00:00",
        )
        conn.commit()
        conn.close()

    def argv(self, *extra: str) -> list[str]:
        return [
            "--state-dir",
            str(self.state_dir),
            "--since",
            SINCE,
            "--output",
            str(self.output),
            *extra,
        ]

    def snapshot_db(self) -> bytes:
        return self.db_path.read_bytes()

    def rules(self) -> dict[str, dict[str, Any]]:
        conn = _connect(self.db_path)
        rows = conn.execute(
            "SELECT rule_id, q_value, q_updates, active, retired_at FROM global_rules"
        ).fetchall()
        conn.close()
        return {str(row["rule_id"]): dict(row) for row in rows}

    def citations(self) -> list[dict[str, Any]]:
        conn = _connect(self.db_path)
        rows = conn.execute(
            "SELECT decision_id, rule_ids, ts, verdict, reward, forward_return, "
            "evaluated_at, outcome_semantics_version FROM global_rule_citations "
            "ORDER BY decision_id"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def notes(self) -> dict[str, Any]:
        conn = _connect(self.db_path)
        row = conn.execute("SELECT q_value, q_updates FROM notes WHERE id=1").fetchone()
        conn.close()
        assert row is not None
        return dict(row)

    def report(self) -> dict[str, Any]:
        return json.loads(self.output.read_text(encoding="utf-8"))

    def journal_mode(self) -> str:
        conn = _connect(self.db_path)
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        conn.close()
        return mode


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _run(env: Env, *extra: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    if kwargs:
        report = repair.run_repair(
            state_dir=env.state_dir,
            since=datetime.fromisoformat(SINCE),
            output=env.output,
            apply="--apply" in extra,
            **kwargs,
        )
        code = 0 if report["status"] != "refused" else 2
        return code, report
    code = repair.main(env.argv(*extra))
    return code, env.report()


def test_script_stays_on_stdlib_sqlite_and_does_not_migrate() -> None:
    source = Path(repair.__file__).read_text(encoding="utf-8")
    assert "LearningsStore" not in source
    assert "ConsolidatedLearningsStore" not in source
    assert "VACUUM" not in source.upper()
    assert "journal_mode" not in source
    assert "INSERT OR REPLACE" not in source.upper()
    assert "os.kill" in source
    assert "mode=ro" in source
    assert "mode=rw" in source
    assert "BEGIN IMMEDIATE" in source


def test_dry_run_plans_eligible_skips_ineligible_and_does_not_mutate(
    env: Env, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, bool | None]] = []
    real_connect = sqlite3.connect

    def wrapped(dsn: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        seen.append((str(dsn), kwargs.get("uri") if isinstance(kwargs.get("uri"), bool) else None))
        return real_connect(dsn, *args, **kwargs)

    monkeypatch.setattr(repair.sqlite3, "connect", wrapped)

    eligible = _llm_row("dec-eligible", cycle_ts="2026-09-04T00:00:00+00:00")
    infra = _llm_row(
        "dec-infra",
        cycle_ts="2026-09-04T01:00:00+00:00",
        decision_source="infra",
        model_called=False,
        decision={
            "decision_id": "dec-infra",
            "decision_source": "infra",
            "model_called": False,
        },
    )
    errored = _llm_row(
        "dec-error",
        cycle_ts="2026-09-04T02:00:00+00:00",
        llm_error="bad_output",
    )
    dry = _llm_row(
        "dec-dry",
        cycle_ts="2026-09-04T03:00:00+00:00",
        dry_run=True,
        runtime={"dry_run": True},
    )
    empty = _llm_row(
        "dec-empty",
        cycle_ts="2026-09-04T04:00:00+00:00",
        rule_ids=[],
        decision={"decision_id": "dec-empty", "applied_learning_ids": []},
    )
    wrong_field = _llm_row(
        "dec-wrong-field",
        cycle_ts="2026-09-04T05:00:00+00:00",
        rule_ids=[],
    )
    wrong_field.pop("applied_learning_ids")
    wrong_field["applied_learning_rule_ids"] = ["rule-fees"]
    wrong_field["decision"] = {"applied_learning_rule_ids": ["rule-fees"]}
    old = _llm_row("dec-old", cycle_ts="2026-09-03T23:59:59+00:00")
    lines = _write_jsonl(
        env.ledger,
        [eligible, infra, errored, dry, empty, wrong_field, old],
    )
    before_db = env.snapshot_db()
    before_ledger = env.ledger.read_bytes()
    before_mode = env.journal_mode()
    before_rules = env.rules()
    before_notes = env.notes()

    code, report = _run(env)
    stdout = capsys.readouterr().out

    assert code == 0
    assert report["status"] == "dry_run"
    assert report["summary"]["changed"] == 0
    assert report["summary"]["planned_insertions"] == 1
    assert report["summary"]["conflicts"] == 0
    assert [item["decision_id"] for item in report["proposal"]] == ["dec-eligible"]
    assert report["proposal"][0]["rule_ids"] == ["rule-fees"]
    assert report["proposal"][0]["cycle_ts"] == "2026-09-04T00:00:00+00:00"
    skip_reasons = {item["reason"] for item in report["skipped"]}
    assert {
        "skipped_not_llm",
        "skipped_llm_error",
        "skipped_dry_run",
        "skipped_empty_citations",
        "skipped_before_since",
    } <= skip_reasons
    assert all(item["decision_id"] != "dec-wrong-field" or item["reason"] == "skipped_empty_citations" for item in report["skipped"])
    assert report["proposal"][0]["sources"][0]["sha256"] == _sha256_line(lines[0])
    assert report["proposal"][0]["sources"][0]["line"] == 1
    assert Path(report["proposal"][0]["sources"][0]["path"]) == env.ledger.resolve()
    assert env.snapshot_db() == before_db
    assert env.ledger.read_bytes() == before_ledger
    assert env.journal_mode() == before_mode
    assert env.rules() == before_rules
    assert env.notes() == before_notes
    assert env.citations() == []
    assert "planned_insertions=1" in stdout
    assert "changed=0" in stdout
    uri_seen = [(dsn, uri) for dsn, uri in seen if uri is True]
    assert uri_seen
    assert all("mode=ro" in dsn for dsn, _uri in uri_seen)
    assert not any("mode=rw" in dsn for dsn, _uri in uri_seen)
    assert not list(env.state_dir.glob("learnings.db-wal"))
    assert not list(env.state_dir.glob("learnings.db-shm"))


def test_archived_gzip_is_deduplicated_and_keeps_every_source_hash(env: Env) -> None:
    row = _llm_row("dec-archived", cycle_ts="2026-09-04T06:00:00+00:00", rule_ids=["rule-fees"])
    gz_lines = _write_gzip_jsonl(env.archive_dir / "decisions-2026-08.jsonl.gz", [row])
    live_lines = _write_jsonl(env.ledger, [row])

    code, report = _run(env)
    assert code == 0
    assert report["summary"]["planned_insertions"] == 1
    sources = report["proposal"][0]["sources"]
    assert len(sources) == 2
    paths = {Path(item["path"]) for item in sources}
    assert paths == {
        (env.archive_dir / "decisions-2026-08.jsonl.gz").resolve(),
        env.ledger.resolve(),
    }
    hashes = {item["sha256"] for item in sources}
    assert hashes == {_sha256_line(gz_lines[0]), _sha256_line(live_lines[0])}
    assert {item["line"] for item in sources} == {1}


def test_apply_restores_inactive_known_rule_with_null_outcomes_and_no_lifecycle_change(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real_connect = sqlite3.connect

    def wrapped(dsn: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        seen.append(str(dsn))
        return real_connect(dsn, *args, **kwargs)

    monkeypatch.setattr(repair.sqlite3, "connect", wrapped)
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-retired", cycle_ts="2026-09-04T07:00:00+00:00", rule_ids=["rule-retired"])],
    )
    before_rules = env.rules()
    before_notes = env.notes()
    before_mode = env.journal_mode()

    code, report = _run(env, "--apply")

    assert code == 0
    assert report["status"] == "applied"
    assert report["summary"]["changed"] == 1
    assert report["summary"]["inserted"] == 1
    assert report["plan"][0]["decision_id"] == "dec-retired"
    citations = env.citations()
    assert len(citations) == 1
    assert citations[0]["decision_id"] == "dec-retired"
    assert json.loads(citations[0]["rule_ids"]) == ["rule-retired"]
    assert citations[0]["ts"] == "2026-09-04T07:00:00+00:00"
    assert citations[0]["verdict"] is None
    assert citations[0]["reward"] is None
    assert citations[0]["forward_return"] is None
    assert citations[0]["evaluated_at"] is None
    assert citations[0]["outcome_semantics_version"] is None
    assert env.rules() == before_rules
    assert env.rules()["rule-retired"]["active"] == 0
    assert env.notes() == before_notes
    assert env.journal_mode() == before_mode
    assert any("mode=rw" in dsn for dsn in seen)


def test_unknown_rule_divergent_duplicate_and_mismatched_existing_refuse_apply(env: Env) -> None:
    conn = _connect(env.db_path)
    _insert_citation(conn, "dec-existing", ["rule-fees"], "2026-09-04T08:00:00+00:00")
    conn.commit()
    conn.close()
    _write_jsonl(
        env.ledger,
        [
            _llm_row(
                "dec-unknown",
                cycle_ts="2026-09-04T09:00:00+00:00",
                rule_ids=["rule-fees", "rule-ghost"],
            ),
            _llm_row("dec-divergent", cycle_ts="2026-09-04T10:00:00+00:00", rule_ids=["rule-fees"]),
            _llm_row(
                "dec-divergent",
                cycle_ts="2026-09-04T10:00:00+00:00",
                rule_ids=["rule-retired"],
            ),
            _llm_row(
                "dec-existing",
                cycle_ts="2026-09-04T08:05:00+00:00",
                rule_ids=["rule-fees"],
            ),
        ],
    )
    before = env.snapshot_db()

    code, report = _run(env, "--apply")

    assert code == 2
    assert report["status"] == "refused"
    reasons = {item["reason"] for item in report["conflicts"]}
    assert "conflict_unknown_rule" in reasons
    assert "conflict_divergent_duplicate" in reasons
    assert "conflict_existing_mismatch" in reasons
    assert env.snapshot_db() == before
    unknown = next(item for item in report["conflicts"] if item["reason"] == "conflict_unknown_rule")
    assert unknown["rule_ids"] == ["rule-fees", "rule-ghost"]


def test_already_present_matches_instant_and_ordered_membership(env: Env) -> None:
    conn = _connect(env.db_path)
    _insert_citation(conn, "dec-same", ["rule-fees"], "2026-09-04T11:00:00+00:00")
    conn.commit()
    conn.close()
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-same", cycle_ts="2026-09-04T12:00:00+01:00", rule_ids=["rule-fees"])],
    )

    code, report = _run(env)
    assert code == 0
    assert report["summary"]["planned_insertions"] == 0
    assert report["summary"]["already_present"] == 1
    assert report["already_present"][0]["decision_id"] == "dec-same"


def test_chronology_timezone_offsets_and_preflight_txn_race_refuse(env: Env) -> None:
    conn = _connect(env.db_path)
    _insert_citation(
        conn,
        "dec-later-eval",
        ["rule-fees"],
        "2026-09-06T01:00:00+01:00",
        verdict="WIN",
        reward=1.0,
        forward_return=0.02,
        evaluated_at="2026-09-07T00:00:00+00:00",
        outcome_semantics_version=3,
    )
    conn.commit()
    conn.close()
    _write_jsonl(
        env.ledger,
        [
            _llm_row(
                "dec-earlier",
                cycle_ts="2026-09-05T23:00:00-01:00",
                rule_ids=["rule-fees"],
            )
        ],
    )
    before = env.snapshot_db()
    code, report = _run(env, "--apply")
    assert code == 2
    assert any(item["reason"] == "conflict_chronology" for item in report["conflicts"])
    assert env.snapshot_db() == before

    conn = _connect(env.db_path)
    conn.execute("DELETE FROM global_rule_citations")
    _insert_citation(conn, "dec-later-pending", ["rule-fees"], "2026-09-06T00:00:00+00:00")
    conn.commit()
    conn.close()
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-race", cycle_ts="2026-09-05T00:00:00+00:00", rule_ids=["rule-fees"])],
    )
    dry_code, dry_report = _run(env)
    assert dry_code == 0
    assert dry_report["summary"]["planned_insertions"] == 1
    assert dry_report["summary"]["conflicts"] == 0

    def evaluate_later() -> None:
        conn_rw = _connect(env.db_path)
        conn_rw.execute(
            """
            UPDATE global_rule_citations
            SET verdict='LOSS', reward=-1.0, forward_return=-0.01,
                evaluated_at='2026-09-08T00:00:00+00:00',
                outcome_semantics_version=3
            WHERE decision_id='dec-later-pending'
            """
        )
        conn_rw.commit()
        conn_rw.close()

    before_apply = env.citations()
    code, report = _run(env, "--apply", before_write_transaction=evaluate_later)
    assert code == 2
    assert report["status"] == "refused"
    assert any(item["reason"] == "conflict_chronology" for item in report["conflicts"])
    after = {row["decision_id"]: row for row in env.citations()}
    assert "dec-race" not in after
    assert after["dec-later-pending"]["reward"] == -1.0
    assert after["dec-later-pending"]["evaluated_at"] == "2026-09-08T00:00:00+00:00"
    assert len(before_apply) == 1


def test_source_mutation_before_txn_refuses(env: Env) -> None:
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-mutate", cycle_ts="2026-09-04T12:00:00+00:00", rule_ids=["rule-fees"])],
    )

    def mutate() -> None:
        _write_jsonl(
            env.ledger,
            [_llm_row("dec-mutate", cycle_ts="2026-09-04T12:00:00+00:00", rule_ids=["rule-retired"])],
        )

    code, report = _run(env, "--apply", before_write_transaction=mutate)
    assert code == 2
    assert report["status"] == "refused"
    assert report["refused_reason"] == "source_changed"
    assert env.citations() == []


def test_schema_unknown_metadata_and_missing_unique_refuse(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-schema", cycle_ts="2026-09-04T13:00:00+00:00")],
    )
    conn = _connect(env.db_path)
    conn.execute("UPDATE learnings_metadata SET value='2' WHERE key='outcome_semantics_version'")
    conn.commit()
    conn.close()
    code, report = _run(env)
    assert code == 2
    assert report["refused_reason"] == "schema_invalid"

    conn = _connect(env.db_path)
    conn.execute("DELETE FROM learnings_metadata")
    conn.commit()
    conn.close()
    code, report = _run(env)
    assert code == 2
    assert report["refused_reason"] == "schema_invalid"

    conn = _connect(env.db_path)
    conn.execute(
        "INSERT INTO learnings_metadata(key, value, updated_at) VALUES (?, ?, ?)",
        ("outcome_semantics_version", "3", "2026-09-01T00:00:00+00:00"),
    )
    conn.execute("ALTER TABLE global_rule_citations ADD COLUMN extra TEXT")
    conn.commit()
    conn.close()
    code, report = _run(env)
    assert code == 2
    assert report["refused_reason"] == "schema_invalid"

    broken = tmp_path / "broken"
    broken.mkdir()
    db_path = broken / "learnings.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        RULES_DDL
        + METADATA_DDL
        + """
        CREATE TABLE global_rule_citations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL,
            rule_ids TEXT NOT NULL,
            ts TEXT NOT NULL,
            verdict TEXT,
            reward REAL,
            forward_return REAL,
            evaluated_at TEXT,
            outcome_semantics_version INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO learnings_metadata(key, value, updated_at) VALUES (?, ?, ?)",
        ("outcome_semantics_version", "3", "2026-09-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO global_rules(rule_id, q_value, q_updates, active) VALUES ('rule-fees', 0, 0, 1)"
    )
    conn.commit()
    conn.close()
    ledger = broken / "decisions.jsonl"
    _write_jsonl(ledger, [_llm_row("dec-unique", cycle_ts="2026-09-04T13:00:00+00:00")])
    output = tmp_path / "broken-audit.json"
    code = repair.main(
        ["--state-dir", str(broken), "--since", SINCE, "--output", str(output)]
    )
    assert code == 2
    assert json.loads(output.read_text(encoding="utf-8"))["refused_reason"] == "schema_invalid"


def test_live_and_malformed_daemon_guard_blocks_apply_not_dry_run(env: Env) -> None:
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-daemon", cycle_ts="2026-09-04T14:00:00+00:00")],
    )
    (env.state_dir / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
    dry_code, dry_report = _run(env)
    assert dry_code == 0
    assert dry_report["status"] == "dry_run"

    code, report = _run(env, "--apply")
    assert code == 2
    assert report["status"] == "refused"
    assert report["refused_reason"] == "daemon_not_stopped"
    assert env.citations() == []

    (env.state_dir / "daemon.pid").unlink()
    (env.state_dir / "daemon_status.json").write_text("{", encoding="utf-8")
    code, report = _run(env, "--apply")
    assert code == 2
    assert report["refused_reason"] == "daemon_not_stopped"

    (env.state_dir / "daemon_status.json").write_text(
        json.dumps({"pid": 2_147_483_647}), encoding="utf-8"
    )
    code, report = _run(env, "--apply")
    assert code == 0
    assert report["status"] == "applied"

    def fail_probe(pid: int) -> bool:
        raise PermissionError("denied")

    conn = _connect(env.db_path)
    conn.execute("DELETE FROM global_rule_citations")
    conn.commit()
    conn.close()
    (env.state_dir / "daemon.pid").write_text("4242", encoding="utf-8")
    code, report = _run(env, "--apply", pid_probe=fail_probe)
    assert code == 2
    assert report["refused_reason"] == "daemon_not_stopped"
    assert env.citations() == []


def test_second_apply_is_noop_and_leaves_existing_outcomes_and_q(env: Env) -> None:
    conn = _connect(env.db_path)
    _insert_citation(
        conn,
        "dec-evaluated",
        ["rule-fees"],
        "2026-09-04T00:30:00+00:00",
        verdict="WIN",
        reward=1.0,
        forward_return=0.04,
        evaluated_at="2026-09-05T00:00:00+00:00",
        outcome_semantics_version=3,
    )
    conn.execute("UPDATE global_rules SET q_value=0.8, q_updates=12 WHERE rule_id='rule-fees'")
    conn.commit()
    conn.close()
    _write_jsonl(
        env.ledger,
        [
            _llm_row("dec-evaluated", cycle_ts="2026-09-04T00:30:00+00:00", rule_ids=["rule-fees"]),
            _llm_row("dec-pending", cycle_ts="2026-09-08T00:00:00+00:00", rule_ids=["rule-fees"]),
        ],
    )

    code, report = _run(env, "--apply")
    assert code == 0
    assert report["summary"]["inserted"] == 1
    first_citations = env.citations()
    first_rules = env.rules()
    evaluated = next(row for row in first_citations if row["decision_id"] == "dec-evaluated")
    assert evaluated["reward"] == 1.0
    assert evaluated["verdict"] == "WIN"
    assert first_rules["rule-fees"]["q_value"] == 0.8
    assert first_rules["rule-fees"]["q_updates"] == 12

    code, report = _run(env, "--apply")
    assert code == 0
    assert report["summary"]["inserted"] == 0
    assert report["summary"]["changed"] == 0
    assert report["summary"]["planned_insertions"] == 0
    assert report["summary"]["already_present"] == 2
    assert env.citations() == first_citations
    assert env.rules() == first_rules


def test_output_inside_state_dir_or_on_sources_is_refused(env: Env) -> None:
    _write_jsonl(env.ledger, [_llm_row("dec-out", cycle_ts="2026-09-04T15:00:00+00:00")])
    code = repair.main(
        [
            "--state-dir",
            str(env.state_dir),
            "--since",
            SINCE,
            "--output",
            str(env.state_dir / "audit.json"),
        ]
    )
    assert code == 2
    assert not (env.state_dir / "audit.json").exists()
    assert env.citations() == []

    code = repair.main(
        [
            "--state-dir",
            str(env.state_dir),
            "--since",
            SINCE,
            "--output",
            str(env.db_path),
        ]
    )
    assert code == 2


def test_absent_db_and_naive_since_refuse(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.db_path.unlink()
    _write_jsonl(env.ledger, [_llm_row("dec-missing-db", cycle_ts="2026-09-04T16:00:00+00:00")])
    code = repair.main(env.argv())
    assert code == 2
    assert env.output.exists()
    assert json.loads(env.output.read_text(encoding="utf-8"))["refused_reason"] == "database_missing"

    with pytest.raises(SystemExit):
        repair.main(
            [
                "--state-dir",
                str(env.state_dir),
                "--since",
                "2026-09-04T00:00:00",
                "--output",
                str(env.root / "naive.json"),
            ]
        )


def test_nested_field_disagreement_is_conflict(env: Env) -> None:
    row = _llm_row("dec-nested", cycle_ts="2026-09-04T17:00:00+00:00")
    row["decision"]["decision_id"] = "other-id"
    _write_jsonl(env.ledger, [row])
    code, report = _run(env, "--apply")
    assert code == 2
    assert any(item["reason"] == "conflict_nested_field_mismatch" for item in report["conflicts"])
    assert env.citations() == []


def test_proposal_is_sorted_by_utc_then_decision_id(env: Env) -> None:
    _write_jsonl(
        env.ledger,
        [
            _llm_row("dec-b", cycle_ts="2026-09-04T18:00:00+00:00"),
            _llm_row("dec-a", cycle_ts="2026-09-04T20:00:00+02:00"),
            _llm_row("dec-c", cycle_ts="2026-09-04T19:00:00+00:00"),
        ],
    )
    _code, report = _run(env)
    assert [item["decision_id"] for item in report["proposal"]] == ["dec-a", "dec-b", "dec-c"]


def test_present_empty_daemon_identity_is_refused(env: Env) -> None:
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-empty-daemon", cycle_ts="2026-09-04T14:30:00+00:00")],
    )

    (env.state_dir / "daemon.pid").write_text("", encoding="utf-8")
    code, report = _run(env, "--apply")
    assert code == 2
    assert report["status"] == "refused"
    assert report["refused_reason"] == "daemon_not_stopped"
    assert report["summary"]["changed"] == 0
    assert env.citations() == []
    (env.state_dir / "daemon.pid").unlink()

    (env.state_dir / "daemon_status.json").write_text("{}", encoding="utf-8")
    code, report = _run(env, "--apply")
    assert code == 2
    assert report["refused_reason"] == "daemon_not_stopped"
    assert env.citations() == []

    (env.state_dir / "daemon_status.json").write_text(
        json.dumps({"pid": None}), encoding="utf-8"
    )
    code, report = _run(env, "--apply")
    assert code == 2
    assert report["refused_reason"] == "daemon_not_stopped"
    assert env.citations() == []


def test_daemon_pid_becoming_live_after_preflight_refuses_inside_transaction(
    env: Env,
) -> None:
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-daemon-race", cycle_ts="2026-09-04T14:45:00+00:00")],
    )
    (env.state_dir / "daemon.pid").write_text("4242\n", encoding="utf-8")
    live = {"on": False}

    def probe(pid: int) -> bool:
        assert pid == 4242
        return live["on"]

    def become_live() -> None:
        live["on"] = True

    code, report = _run(
        env, "--apply", pid_probe=probe, before_write_transaction=become_live
    )
    assert code == 2
    assert report["status"] == "refused"
    assert report["refused_reason"] == "daemon_not_stopped"
    assert report["summary"]["changed"] == 0
    assert env.citations() == []


def test_insert_nulls_outcomes_even_when_schema_defaults_are_non_null(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    conn = _connect(env.db_path)
    conn.execute("DROP TABLE global_rule_citations")
    conn.executescript(
        """
        CREATE TABLE global_rule_citations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL UNIQUE,
            rule_ids TEXT NOT NULL,
            ts TEXT NOT NULL,
            verdict TEXT DEFAULT 'WIN',
            reward REAL DEFAULT 1.0,
            forward_return REAL DEFAULT 0.99,
            evaluated_at TEXT DEFAULT '1999-01-01T00:00:00+00:00',
            outcome_semantics_version INTEGER DEFAULT 3
        );
        """
    )
    conn.commit()
    conn.close()
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-defaults", cycle_ts="2026-09-04T15:30:00+00:00")],
    )
    before_rules = env.rules()
    before_notes = env.notes()

    code, report = _run(env, "--apply")

    assert code == 0
    assert report["status"] == "applied"
    assert report["summary"]["inserted"] == 1
    citations = env.citations()
    assert len(citations) == 1
    assert citations[0]["decision_id"] == "dec-defaults"
    assert citations[0]["verdict"] is None
    assert citations[0]["reward"] is None
    assert citations[0]["forward_return"] is None
    assert citations[0]["evaluated_at"] is None
    assert citations[0]["outcome_semantics_version"] is None
    assert env.rules() == before_rules
    assert env.notes() == before_notes


def test_identical_citation_inserted_before_txn_is_applied_noop(env: Env) -> None:
    cycle_ts = "2026-09-04T16:30:00+00:00"
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-race-present", cycle_ts=cycle_ts, rule_ids=["rule-fees"])],
    )

    def insert_same() -> None:
        conn = _connect(env.db_path)
        _insert_citation(conn, "dec-race-present", ["rule-fees"], cycle_ts)
        conn.commit()
        conn.close()

    code, report = _run(env, "--apply", before_write_transaction=insert_same)

    assert code == 0
    assert report["status"] == "applied"
    assert report["refused_reason"] is None
    assert [item["decision_id"] for item in report["proposal"]] == ["dec-race-present"]
    assert [item["decision_id"] for item in report["plan"]] == ["dec-race-present"]
    assert report["summary"]["planned_insertions"] == 1
    assert report["summary"]["inserted"] == 0
    assert report["summary"]["changed"] == 0
    assert report["summary"]["already_present"] == 1
    assert report["inserted"] == []
    assert [item["decision_id"] for item in report["already_present"]] == [
        "dec-race-present"
    ]
    citations = env.citations()
    assert [row["decision_id"] for row in citations] == ["dec-race-present"]
    assert json.loads(citations[0]["rule_ids"]) == ["rule-fees"]
    assert citations[0]["ts"] == cycle_ts


def test_busy_begin_immediate_writes_refused_audit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_jsonl(
        env.ledger,
        [_llm_row("dec-busy", cycle_ts="2026-09-04T17:30:00+00:00")],
    )
    real_connect_db = repair.connect_db

    class FailBeginConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql: object, *params: object) -> sqlite3.Cursor:
            if str(sql).strip().upper() == "BEGIN IMMEDIATE":
                raise sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, *params)

        def close(self) -> None:
            self._inner.close()

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    def wrapped(path: Path, *, mode: str) -> sqlite3.Connection:
        conn = real_connect_db(path, mode=mode)
        if mode != "rw":
            return conn
        return FailBeginConnection(conn)  # type: ignore[return-value]

    monkeypatch.setattr(repair, "connect_db", wrapped)
    before = env.snapshot_db()

    code, report = _run(env, "--apply")

    assert code == 2
    assert report["status"] == "refused"
    assert report["refused_reason"] == "busy"
    assert report["summary"]["changed"] == 0
    assert env.output.exists()
    assert env.snapshot_db() == before
    assert env.citations() == []
