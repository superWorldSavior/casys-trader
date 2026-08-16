"""Extraction incrémentale llm_cost + hook rétention avant purge sqlite."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import archive_agent_storage, llm_cost


def _write_logs_db(path: Path, *, body: str | None, with_token_cols: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    if with_token_cols:
        conn.execute(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, input_tokens INTEGER, "
            "output_tokens INTEGER, cached_tokens INTEGER)"
        )
        conn.execute("INSERT INTO logs VALUES (1, 1786550000, 100, 20, 10)")
    else:
        conn.execute(
            "CREATE TABLE logs ("
            "id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, level TEXT, "
            "target TEXT, feedback_log_body TEXT, module_path TEXT, file TEXT, "
            "line INTEGER, thread_id TEXT, process_uuid TEXT, estimated_bytes INTEGER)"
        )
        if body is not None:
            conn.execute(
                "INSERT INTO logs VALUES (7, 1786550000, 0, 'INFO', 'codex', ?, '', '', 0, '', '', 0)",
                (body,),
            )
    conn.commit()
    conn.close()


def _write_state_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, model TEXT, "
        "tokens_used INTEGER, created_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO threads VALUES ('thr-1', 'openai', 'gpt-5.6-luna', 40943, 1786122519)"
    )
    conn.execute(
        "INSERT INTO threads VALUES ('thr-0', 'openai', 'gpt-5.6-sol', 0, 1786122519)"
    )
    conn.commit()
    conn.close()


def test_inspect_sqlite_detecte_l_absence_de_colonnes_tokens(tmp_path: Path) -> None:
    db = tmp_path / "logs_2.sqlite"
    _write_logs_db(db, body=None)
    report = llm_cost.inspect_sqlite(db)
    assert report["ok"] is True
    assert report["has_token_columns"] is False
    assert report["tables"]["logs"]["token_columns"] == []


def test_extract_parse_le_body_des_logs_et_les_threads(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    _write_logs_db(
        home / "logs_2.sqlite",
        body=json.dumps(
            {
                "input_tokens": 111,
                "output_tokens": 22,
                "cached_tokens": 5,
                "total_tokens": 138,
            }
        ),
    )
    _write_state_db(home / "state_5.sqlite")
    rows = list(llm_cost.extract_sqlite_rows(home))
    by_source = {row.source: row for row in rows}
    assert by_source["codex_logs"].input_tokens == 111
    assert by_source["codex_logs"].output_tokens == 22
    assert by_source["codex_thread"].total_tokens == 40943
    assert by_source["codex_thread"].grain == "thread"


def test_extract_acpx_request_et_proxy_chars(tmp_path: Path) -> None:
    sessions = tmp_path / "acpx"
    sessions.mkdir()
    (sessions / "sess-tok.json").write_text(
        json.dumps(
            {
                "name": "8710:0",
                "created_at": "2026-08-10T05:08:39.307Z",
                "agent_command": "codex",
                "agent_argv": ["codex", "--model", "gpt-5.6-luna"],
                "request_token_usage": {
                    "req-1": {
                        "input_tokens": 31739,
                        "output_tokens": 244,
                        "cache_read_input_tokens": 8960,
                        "thought_tokens": 131,
                        "total_tokens": 40943,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (sessions / "sess-empty.json").write_text(
        json.dumps(
            {
                "name": "grok-1",
                "created_at": "2026-08-16T03:08:17Z",
                "agent_command": "grok",
                "request_token_usage": {},
                "messages": [
                    {"role": "user", "content": "hello world"},
                    {"role": "assistant", "content": "ok"},
                ],
            }
        ),
        encoding="utf-8",
    )
    rows = list(llm_cost.extract_acpx_session_rows(sessions))
    by_id = {row.id: row for row in rows}
    token_row = by_id["acpx_request:sess-tok:req-1"]
    assert token_row.input_tokens == 31739
    assert token_row.cached_tokens == 8960
    assert token_row.provider == "acpx"
    assert token_row.model == "gpt-5.6-luna"
    char_row = by_id["acpx_chars:sess-empty"]
    assert char_row.proxy is True
    assert char_row.chars_prompt == len("hello world")
    assert char_row.chars_response == len("ok")


def test_extract_grok_session_last_usage(tmp_path: Path) -> None:
    session = tmp_path / "grok-home" / "sessions" / "cwd" / "sid-1"
    session.mkdir(parents=True)
    (session / "summary.json").write_text(
        json.dumps({"created_at": "2026-08-16T03:08:17.737095Z", "current_model_id": "grok-4.6"}),
        encoding="utf-8",
    )
    (session / "updates.jsonl").write_text(
        json.dumps({"timestamp": "t1", "params": {"update": {"usage": {"inputTokens": 10, "totalTokens": 12}}}})
        + "\n"
        + json.dumps(
            {
                "timestamp": "t2",
                "params": {
                    "update": {
                        "usage": {
                            "inputTokens": 112468,
                            "outputTokens": 1660,
                            "totalTokens": 114128,
                            "cachedReadTokens": 52096,
                            "reasoningTokens": 1401,
                            "modelCalls": 4,
                            "apiDurationMs": 32322,
                        }
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rows = list(llm_cost.extract_grok_session_rows([tmp_path / "grok-home"]))
    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "grok"
    assert row.input_tokens == 112468
    assert row.cached_tokens == 52096
    assert row.n_calls == 4
    assert row.dur_s == 32.322


def test_extract_and_append_est_idempotent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    log = state / "daemon_console.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "2026-08-13 09:38:19,068 INFO [acpx_call] session=9334:0 symbol=- "
        "pid=75632 provider=universe timeout_s=255 dur_s=2.6 outcome=ok\n",
        encoding="utf-8",
    )
    first = llm_cost.extract_and_append(
        repo_root=tmp_path,
        state_dir=state,
        codex_home=tmp_path / "missing-codex",
        acpx_sessions=tmp_path / "missing-acpx",
        grok_homes=[],
        console_log=log,
    )
    second = llm_cost.extract_and_append(
        repo_root=tmp_path,
        state_dir=state,
        codex_home=tmp_path / "missing-codex",
        acpx_sessions=tmp_path / "missing-acpx",
        grok_homes=[],
        console_log=log,
    )
    assert first["appended"] == 1
    assert second["appended"] == 0
    assert second["skipped"] == 1
    lines = list((state / "archive" / "llm_usage" / "2026-08.jsonl").read_text().splitlines())
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["source"] == "acpx_call_proxy"
    assert row["provider"] == "universe"
    assert row["proxy"] is True
    assert row["dur_s"] == 2.6


def test_archive_extrait_avant_de_purger_les_sqlite(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    home = repo / "ops" / "codex-home"
    _write_logs_db(
        home / "logs_2.sqlite",
        body=json.dumps({"input_tokens": 9, "output_tokens": 1, "total_tokens": 10}),
    )
    monkeypatch.setattr(archive_agent_storage, "REPO_ROOT", repo)
    monkeypatch.setattr(archive_agent_storage, "CODEX_HOME", home)
    monkeypatch.setattr(archive_agent_storage, "ACPX_SESSIONS", tmp_path / "no-sessions")
    monkeypatch.setattr(archive_agent_storage, "ARCHIVE_ROOT", tmp_path / "archives")
    monkeypatch.setattr(
        llm_cost,
        "DEFAULT_ACPX_SESSIONS",
        tmp_path / "no-sessions",
    )

    order: list[str] = []
    real_extract = llm_cost.extract_and_append
    real_purge = archive_agent_storage.purge_codex_logs

    def tracked_extract(**kwargs):
        order.append("extract")
        kwargs.setdefault("repo_root", repo)
        kwargs.setdefault("state_dir", repo / "state")
        kwargs.setdefault("codex_home", home)
        kwargs.setdefault("acpx_sessions", tmp_path / "no-sessions")
        kwargs.setdefault("grok_homes", [])
        kwargs.setdefault("console_log", repo / "state" / "daemon_console.log")
        return real_extract(**kwargs)

    def tracked_purge(db_home, *, apply):
        order.append("purge")
        return real_purge(db_home, apply=apply)

    monkeypatch.setattr(llm_cost, "extract_and_append", tracked_extract)
    monkeypatch.setattr(archive_agent_storage, "purge_codex_logs", tracked_purge)

    assert archive_agent_storage.main(["--only", "logs", "--apply"]) == 0
    assert order == ["extract", "purge"]

    usage_files = list((repo / "state" / "archive" / "llm_usage").glob("*.jsonl"))
    assert usage_files
    rows = [
        json.loads(line)
        for path in usage_files
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["input_tokens"] == 9
    conn = sqlite3.connect(str(home / "logs_2.sqlite"))
    assert conn.execute("SELECT count(*) FROM logs").fetchone()[0] == 0
    conn.close()
