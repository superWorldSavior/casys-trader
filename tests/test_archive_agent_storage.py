from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import archive_agent_storage
from trader.domain.agent_storage_retention import (
    PendingReconciliationJournal,
    QuarantineTransactionIntent,
    SessionDocsVacuumJournal,
)


def _install_identity_zstd(monkeypatch) -> None:
    monkeypatch.setattr(archive_agent_storage, "resolve_zstd", lambda: "zstd")

    def fake_run(argv, *, check, **_kwargs):
        assert check is True
        if "-o" in argv:
            shutil.copyfile(argv[-1], Path(argv[argv.index("-o") + 1]))

    monkeypatch.setattr(archive_agent_storage.subprocess, "run", fake_run)


def _old_timestamp() -> float:
    return (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()


def _touch_old(path: Path) -> None:
    os.utime(path, (_old_timestamp(), _old_timestamp()))


def test_archive_month_choisit_un_nom_libre_sans_ecraser(tmp_path, monkeypatch) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    destination = tmp_path / "archive" / "acpx-sessions-2026-08.tar.zst"
    destination.parent.mkdir()
    destination.write_bytes(b"archive-zero")
    destination.with_name("acpx-sessions-2026-08.1.tar.zst").write_bytes(b"archive-one")

    _install_identity_zstd(monkeypatch)

    result = archive_agent_storage.archive_month([source], destination, apply=True)

    published = destination.with_name("acpx-sessions-2026-08.2.tar.zst")
    assert result["archive"] == str(published)
    assert published.is_file()
    assert destination.read_bytes() == b"archive-zero"
    assert destination.with_name("acpx-sessions-2026-08.1.tar.zst").read_bytes() == b"archive-one"
    assert published.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert not source.exists()


def test_dry_run_ne_lance_pas_extraction_et_necrit_aucune_archive(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    calls: list[str] = []
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source = sessions / "old-session.json"
    source.write_text(json.dumps({"closed": True, "acpx_record_id": "old-session"}), encoding="utf-8")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(source, (old_ts, old_ts))
    archive_root = tmp_path / "archives"
    monkeypatch.setattr(archive_agent_storage, "ACPX_SESSIONS", sessions)
    monkeypatch.setattr(archive_agent_storage, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(archive_agent_storage, "CODEX_HOME", tmp_path / "codex-home")
    monkeypatch.setattr(
        archive_agent_storage,
        "_extract_llm_usage_before_purge",
        lambda: calls.append("extract"),
    )

    assert archive_agent_storage.main([]) == 0

    report = json.loads(capsys.readouterr().out)
    assert calls == []
    assert report["llm_usage"] == {"applied": False, "appended": 0, "reason": "dry_run"}
    assert report["sessions"][0]["applied"] is False
    assert json.loads(source.read_text(encoding="utf-8"))["closed"] is True
    assert not archive_root.exists()


def test_acpx_archive_group_requires_closed_record_and_keeps_stream_together(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    closed = root / "closed-id.json"
    closed.write_text(json.dumps({"closed": True, "acpx_record_id": "closed-id"}), encoding="utf-8")
    stream = root / "closed-id.stream.ndjson"
    stream.write_text("event\n", encoding="utf-8")
    opened = root / "open-id.json"
    opened.write_text(json.dumps({"closed": False, "acpx_record_id": "open-id"}), encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    for path in (closed, stream, opened):
        os.utime(path, (old, old))
    source = archive_agent_storage.ArchiveSource("acpx", root, "sessions", "acpx_sessions")

    grouped, protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
    )

    units = [unit for values in grouped.values() for unit in values]
    assert {unit.path.name for unit in units} == {"closed-id.json", "closed-id.stream.ndjson"}
    assert {unit.identity for unit in units} == {"closed-id"}
    assert any(entry["path"] == str(opened) and "not closed" in entry["reason"] for entry in protected)


def test_grok_collection_archives_complete_nested_session_and_protects_active(tmp_path: Path) -> None:
    home = tmp_path / "grok-home"
    sessions = home / "sessions"
    workspace = sessions / "%2Frepo"
    closed_id = "01a00000-0000-7000-8000-000000000001"
    active_id = "01a00000-0000-7000-8000-000000000002"
    for session_id in (closed_id, active_id):
        session = workspace / session_id
        (session / "prompts").mkdir(parents=True)
        (session / "summary.json").write_text("{}", encoding="utf-8")
        (session / "prompts" / "prompt_0.txt").write_text("prompt", encoding="utf-8")
    (home / "active_sessions.json").write_text(json.dumps([{"session_id": active_id}]), encoding="utf-8")
    (home / "active_sessions.lock").touch()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    for path in sorted(workspace.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        os.utime(path, (old, old))
    os.utime(workspace, (old, old))
    source = archive_agent_storage.ArchiveSource("grok-home", sessions, "sessions", "grok_sessions")

    grouped, protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
    )

    units = [unit for values in grouped.values() for unit in values]
    assert len(units) == 1
    assert units[0].path.name == closed_id
    assert units[0].arcname == f"grok-home/sessions/%2Frepo/{closed_id}"
    assert any(entry["path"].endswith(active_id) and entry["reason"] == "active session" for entry in protected)


def test_grok_registry_corrupt_protects_every_session(tmp_path: Path) -> None:
    home = tmp_path / "grok-home"
    session = home / "sessions" / "%2Frepo" / "01a00000-0000-7000-8000-000000000001"
    session.mkdir(parents=True)
    (session / "summary.json").write_text("{}", encoding="utf-8")
    (home / "active_sessions.json").write_text("not-json", encoding="utf-8")
    (home / "active_sessions.lock").touch()
    source = archive_agent_storage.ArchiveSource("grok-home", home / "sessions", "sessions", "grok_sessions")

    grouped, protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) + timedelta(days=1),
    )

    assert not grouped
    assert protected == [{"path": str(home / "sessions"), "reason": "active session registry unavailable"}]


def test_changed_source_is_archived_but_not_removed(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    destination = tmp_path / "archive" / "sessions.tar.zst"
    _install_identity_zstd(monkeypatch)
    monkeypatch.setattr(archive_agent_storage, "_unit_still_stable", lambda _unit: False)

    result = archive_agent_storage.archive_month([source], destination, apply=True)

    assert source.exists()
    assert source.read_text(encoding="utf-8") == "transcript"
    assert result["applied"] is False
    assert result["removed"] == 0
    assert result["retained"][0]["reason"] == "source changed or became active"


def test_reconcile_grok_metadata_removes_only_archived_identity(tmp_path: Path) -> None:
    home = tmp_path / "grok-home"
    workspace = home / "sessions" / "%2Frepo"
    workspace.mkdir(parents=True)
    (home / "active_sessions.json").write_text("[]", encoding="utf-8")
    (home / "active_sessions.lock").touch()
    history = workspace / "prompt_history.jsonl"
    history.write_text(
        json.dumps({"session_id": "old", "prompt": "old"})
        + "\n"
        + json.dumps({"session_id": "live", "prompt": "live"})
        + "\n",
        encoding="utf-8",
    )
    index = home / "sessions" / "session_search.sqlite"
    with sqlite3.connect(index) as connection:
        connection.execute(
            "CREATE TABLE session_docs (session_id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE session_docs_fts USING fts5("
            "title, content, content='session_docs', content_rowid='rowid')"
        )
        connection.execute(
            "CREATE TRIGGER session_docs_ai AFTER INSERT ON session_docs BEGIN "
            "INSERT INTO session_docs_fts(rowid, title, content) "
            "VALUES (new.rowid, new.title, new.content); END"
        )
        connection.execute(
            "CREATE TRIGGER session_docs_ad AFTER DELETE ON session_docs BEGIN "
            "INSERT INTO session_docs_fts(session_docs_fts, rowid, title, content) "
            "VALUES ('delete', old.rowid, old.title, old.content); END"
        )
        connection.executemany(
            "INSERT INTO session_docs VALUES (?, ?, ?)",
            [("old", "obsolete", "old material"), ("live", "current", "live material")],
        )

    result = archive_agent_storage.reconcile_grok_session_metadata(home, {"old"}, apply=True)

    assert result.get("error") is None
    assert [json.loads(line)["session_id"] for line in history.read_text().splitlines()] == ["live"]
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT session_id FROM session_docs").fetchall() == [("live",)]
        assert connection.execute(
            "SELECT count(*) FROM session_docs_fts WHERE session_docs_fts MATCH 'obsolete'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM session_docs_fts WHERE session_docs_fts MATCH 'current'"
        ).fetchone() == (1,)


def test_inventory_reports_unknown_home_without_touching_operational_roots(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    (ops / "future-home").mkdir(parents=True)
    (ops / "grok-home-medium").mkdir()
    sources, _purges = archive_agent_storage.agent_storage_policy(ops)

    inventory = archive_agent_storage._ops_inventory(ops, sources)

    assert inventory["protected_unknown_homes"] == ["future-home"]
    assert "grok-home-medium" in inventory["discovered_homes"]
    assert str(ops / "model-presets") in inventory["protected_operational_roots"]


def test_secure_archive_storage_repairs_legacy_permissions(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir(mode=0o755)
    archive = root / "legacy.tar.zst"
    archive.write_bytes(b"archive")
    archive.chmod(0o644)

    dry_run = archive_agent_storage.secure_archive_storage(root, apply=False)

    assert dry_run["files_requiring_private_mode"] == 1
    assert dry_run["root_requiring_private_mode"] is True
    assert root.stat().st_mode & 0o777 == 0o755
    assert archive.stat().st_mode & 0o777 == 0o644

    applied = archive_agent_storage.secure_archive_storage(root, apply=True)

    assert applied["applied"] is True
    assert root.stat().st_mode & 0o777 == 0o700
    assert archive.stat().st_mode & 0o777 == 0o600


def test_acpx_index_coherence_requires_exact_surviving_records(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "live.json").write_text("{}", encoding="utf-8")
    (sessions / "index.json").write_text(
        json.dumps(
            {
                "schema": "acpx.session-index.v1",
                "files": ["live.json"],
                "entries": [{"file": "live.json"}],
            }
        ),
        encoding="utf-8",
    )

    assert archive_agent_storage._acpx_index_is_coherent(sessions) == (True, 1)

    (sessions / "new.json").write_text("{}", encoding="utf-8")
    assert archive_agent_storage._acpx_index_is_coherent(sessions) == (False, 2)


def test_reconcile_acpx_index_uses_provider_owned_rebuild(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "live.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(archive_agent_storage, "_resolve_acpx", lambda: "/bin/acpx")

    def fake_run(argv, **kwargs):
        assert argv == ["/bin/acpx", "--format", "json", "sessions", "list", "--local"]
        assert kwargs["check"] is True
        assert kwargs["env"]["PATH"].split(os.pathsep)[0] == "/bin"
        (sessions / "index.json").write_text(
            json.dumps(
                {
                    "schema": "acpx.session-index.v1",
                    "files": ["live.json"],
                    "entries": [{"file": "live.json"}],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(archive_agent_storage.subprocess, "run", fake_run)

    result = archive_agent_storage.reconcile_acpx_session_index(
        sessions,
        {"archived"},
        apply=True,
    )

    assert result["applied"] is True
    assert result["records"] == 1


def test_acpx_child_path_puts_binary_dir_ahead_of_launchd_path(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    path = archive_agent_storage.child_path_for_executable("/opt/homebrew/bin/acpx")
    parts = path.split(os.pathsep)

    assert parts[0] == "/opt/homebrew/bin"
    assert parts.count("/opt/homebrew/bin") == 1
    assert "/usr/bin" in parts
    assert "/bin" in parts


def _install_env_node_acpx(bin_dir: Path, *, index_path: Path | None = None) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if index_path is not None:
        payload = json.dumps(
            {
                "schema": "acpx.session-index.v1",
                "files": ["live.json"],
                "entries": [{"file": "live.json"}],
            }
        )
        node = bin_dir / "node"
        node.write_text(
            f"#!/bin/sh\ncat > {shlex.quote(str(index_path))} << 'EOF'\n{payload}\nEOF\nexit 0\n",
            encoding="utf-8",
        )
        node.chmod(0o755)
    acpx = bin_dir / "acpx"
    acpx.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    acpx.chmod(0o755)
    return acpx


@pytest.mark.skipif(os.name != "posix" or not Path("/usr/bin/env").is_file(), reason="requires /usr/bin/env shebang")
def test_reconcile_acpx_index_succeeds_under_launchd_minimal_path(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "live.json").write_text("{}", encoding="utf-8")
    acpx = _install_env_node_acpx(tmp_path / "opt" / "homebrew" / "bin", index_path=sessions / "index.json")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(archive_agent_storage, "_ACPX_FALLBACK_PATHS", (str(acpx),))

    result = archive_agent_storage.reconcile_acpx_session_index(sessions, {"archived"}, apply=True)

    assert result["applied"] is True
    assert result["records"] == 1
    assert result.get("error") is None


@pytest.mark.skipif(os.name != "posix" or not Path("/usr/bin/env").is_file(), reason="requires /usr/bin/env shebang")
@pytest.mark.skipif(
    shutil.which("node", path="/usr/bin:/bin") is not None,
    reason="system PATH already has node; cannot simulate launchd miss",
)
def test_reconcile_acpx_index_reports_env_node_miss_under_launchd_path(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    acpx = _install_env_node_acpx(tmp_path / "opt" / "homebrew" / "bin")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(archive_agent_storage, "_ACPX_FALLBACK_PATHS", (str(acpx),))

    result = archive_agent_storage.reconcile_acpx_session_index(sessions, {"archived"}, apply=True)

    assert result["applied"] is False
    error = result["error"]
    assert "127" in error
    assert "node" in error.lower()
    assert "self-heal" in error


def test_missing_generated_cache_is_an_apply_noop(tmp_path: Path) -> None:
    source = archive_agent_storage.PurgeSource("future-home", tmp_path / "missing-cache")

    result = archive_agent_storage.purge_generated_files(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
        apply=True,
    )

    assert result["reason"] == "missing"
    assert result["files"] == 0
    assert result["removed"] == 0
    assert result["applied"] is False


def test_jsonl_report_is_one_machine_readable_line(capsys) -> None:
    archive_agent_storage._write_report(
        {"schema_version": "agent_storage_retention.v2", "status": "ok"},
        output_format="jsonl",
    )

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output)["status"] == "ok"


def test_symlinked_ops_parent_is_rejected_even_if_leaf_is_a_real_directory(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    ops.mkdir()
    evil = tmp_path / "evil" / "sessions"
    evil.mkdir(parents=True)
    session = evil / "%2Frepo" / "01a00000-0000-7000-8000-000000000001"
    session.mkdir(parents=True)
    (session / "summary.json").write_text("{}", encoding="utf-8")
    (ops / "grok-home").symlink_to(tmp_path / "evil")
    declared = ops / "grok-home" / "sessions"
    (tmp_path / "evil" / "active_sessions.json").write_text("[]", encoding="utf-8")
    (tmp_path / "evil" / "active_sessions.lock").touch()
    source = archive_agent_storage.ArchiveSource(
        "grok-home",
        declared,
        "sessions",
        "grok_sessions",
        authority_root=ops,
    )

    grouped, protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) + timedelta(days=1),
    )

    assert not grouped
    assert any("symlink" in entry["reason"] or "confined" in entry["reason"] for entry in protected)
    assert (session / "summary.json").read_text(encoding="utf-8") == "{}"


def test_symlinked_declared_root_is_rejected_for_acpx_and_ops(tmp_path: Path) -> None:
    real = tmp_path / "real-sessions"
    real.mkdir()
    record = real / "closed-id.json"
    record.write_text(json.dumps({"closed": True, "acpx_record_id": "closed-id"}), encoding="utf-8")
    _touch_old(record)
    linked = tmp_path / "sessions"
    linked.symlink_to(real)
    source = archive_agent_storage.ArchiveSource(
        "acpx",
        linked,
        "sessions",
        "acpx_sessions",
        authority_root=linked,
        allows_external_root=True,
    )

    grouped, protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
    )

    assert not grouped
    assert any("symlink" in entry["reason"] for entry in protected)
    assert json.loads(record.read_text(encoding="utf-8"))["closed"] is True


def test_acpx_external_root_is_the_only_allowed_escape_from_ops(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    ops.mkdir()
    acpx = tmp_path / "external-acpx" / "sessions"
    acpx.mkdir(parents=True)
    record = acpx / "closed-id.json"
    record.write_text(json.dumps({"closed": True, "acpx_record_id": "closed-id"}), encoding="utf-8")
    _touch_old(record)
    source = archive_agent_storage.ArchiveSource(
        "acpx",
        acpx,
        "sessions",
        "acpx_sessions",
        authority_root=acpx,
        allows_external_root=True,
    )

    grouped, protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
    )

    units = [unit for values in grouped.values() for unit in values]
    assert {unit.path.name for unit in units} == {"closed-id.json"}
    assert protected == []


def test_candidate_symlink_is_not_archived(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    target = tmp_path / "secret.env"
    target.write_text("token=1", encoding="utf-8")
    linked = root / "old.log"
    linked.symlink_to(target)
    _touch_old(linked)
    source = archive_agent_storage.ArchiveSource(
        "grok-home",
        root,
        "logs",
        "recursive_files",
        authority_root=tmp_path,
    )

    grouped, protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
    )

    assert not grouped
    assert any(entry["path"] == str(linked) for entry in protected)
    assert target.read_text(encoding="utf-8") == "token=1"


def test_sqlite_symlink_is_never_deleted_or_vacuumed(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    real = tmp_path / "outside" / "logs_2.sqlite"
    real.parent.mkdir()
    with sqlite3.connect(real) as connection:
        connection.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO logs VALUES (1, 'keep')")
    linked = home / "logs_2.sqlite"
    linked.symlink_to(real)

    result = archive_agent_storage.purge_codex_logs(home, apply=True)

    assert result[0]["delete_committed"] is False
    assert result[0]["vacuumed"] is False
    assert "symlink" in result[0]["error"]
    with sqlite3.connect(real) as connection:
        assert connection.execute("SELECT count(*) FROM logs").fetchall() == [(1,)]


def test_purge_reports_delete_committed_when_vacuum_fails(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    db_path = home / "logs_2.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO logs VALUES (1, 'gone')")

    def fail_vacuum(_path: Path) -> None:
        raise sqlite3.OperationalError("cannot VACUUM")

    monkeypatch.setattr(archive_agent_storage, "_vacuum_sqlite", fail_vacuum)

    result = archive_agent_storage.purge_codex_logs(home, apply=True)

    assert result[0]["delete_committed"] is True
    assert result[0]["vacuumed"] is False
    assert "VACUUM" in result[0]["error"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM logs").fetchone() == (0,)


def test_apply_returns_nonzero_when_sqlite_purge_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    db_path = home / "logs_2.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY)")
    linked = home / "logs_3.sqlite"
    linked.symlink_to(db_path)
    monkeypatch.setattr(archive_agent_storage, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(archive_agent_storage, "CODEX_HOME", home)
    monkeypatch.setattr(archive_agent_storage, "ACPX_SESSIONS", tmp_path / "no-sessions")
    monkeypatch.setattr(archive_agent_storage, "ARCHIVE_ROOT", tmp_path / "archives")
    monkeypatch.setattr(
        archive_agent_storage,
        "_extract_llm_usage_before_purge",
        lambda: {"applied": True, "appended": 0},
    )

    assert archive_agent_storage.main(["--only", "logs", "--apply"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "partial_failure"
    assert any(item.get("error") for item in report["logs"])


def test_held_grok_lock_prevents_quarantine_and_keeps_session(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "grok-home"
    session_id = "01a00000-0000-7000-8000-000000000001"
    session = home / "sessions" / "%2Frepo" / session_id
    session.mkdir(parents=True)
    (session / "summary.json").write_text("keep", encoding="utf-8")
    (home / "active_sessions.json").write_text("[]", encoding="utf-8")
    lock_path = home / "active_sessions.lock"
    lock_path.touch()
    _touch_old(session / "summary.json")
    _touch_old(session)
    source = archive_agent_storage.ArchiveSource(
        "grok-home",
        home / "sessions",
        "sessions",
        "grok_sessions",
        authority_root=tmp_path,
    )
    grouped, _protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
    )
    units = [unit for values in grouped.values() for unit in values]
    destination = tmp_path / "archive" / "grok.tar.zst"
    _install_identity_zstd(monkeypatch)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, time, sys\n"
            f"handle = open({str(lock_path)!r}, 'rb')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        result = archive_agent_storage.archive_month(units, destination, apply=True)
    finally:
        holder.kill()
        holder.wait()

    assert session.exists()
    assert (session / "summary.json").read_text(encoding="utf-8") == "keep"
    assert result["removed"] == 0
    assert result["applied"] is False
    assert any(entry["reason"] == "lifecycle lock unavailable" for entry in result["retained"])


def test_busy_acpx_stream_lock_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    record = root / "closed-id.json"
    record.write_text(json.dumps({"closed": True, "acpx_record_id": "closed-id"}), encoding="utf-8")
    stream = root / "closed-id.stream.ndjson"
    stream.write_text("event\n", encoding="utf-8")
    (root / "closed-id.stream.lock").write_text("held-by-acpx", encoding="utf-8")
    _touch_old(record)
    _touch_old(stream)
    source = archive_agent_storage.ArchiveSource(
        "acpx",
        root,
        "sessions",
        "acpx_sessions",
        authority_root=root,
        allows_external_root=True,
    )
    grouped, _protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
    )
    units = [unit for values in grouped.values() for unit in values]
    _install_identity_zstd(monkeypatch)

    result = archive_agent_storage.archive_month(units, tmp_path / "archive" / "acpx.tar.zst", apply=True)

    assert record.exists()
    assert stream.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["closed"] is True
    assert result["removed"] == 0
    assert result["applied"] is False
    assert any(entry["reason"] == "lifecycle lock unavailable" for entry in result["retained"])


def test_stale_acpx_stream_lock_with_dead_pid_is_recovered(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    record = root / "closed-id.json"
    record.write_text(json.dumps({"closed": True, "acpx_record_id": "closed-id"}), encoding="utf-8")
    stream = root / "closed-id.stream.ndjson"
    stream.write_text("event\n", encoding="utf-8")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    dead_pid = holder.pid
    holder.kill()
    holder.wait()
    (root / "closed-id.stream.lock").write_text(
        json.dumps({"pid": dead_pid, "created_at": datetime.now(timezone.utc).isoformat()}) + "\n",
        encoding="utf-8",
    )
    _touch_old(record)
    _touch_old(stream)
    source = archive_agent_storage.ArchiveSource(
        "acpx",
        root,
        "sessions",
        "acpx_sessions",
        authority_root=root,
        allows_external_root=True,
    )
    grouped, _protected = archive_agent_storage.collect_source_units(
        source,
        datetime.now(timezone.utc) - timedelta(days=7),
    )
    units = [unit for values in grouped.values() for unit in values]
    _install_identity_zstd(monkeypatch)

    result = archive_agent_storage.archive_month(units, tmp_path / "archive" / "acpx.tar.zst", apply=True)

    assert result["applied"] is True
    assert result["removed"] == 2
    assert not record.exists()
    assert not stream.exists()
    assert not (root / "closed-id.stream.lock").exists()


def test_live_acpx_stream_lock_pid_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    record = root / "closed-id.json"
    record.write_text(json.dumps({"closed": True, "acpx_record_id": "closed-id"}), encoding="utf-8")
    stream = root / "closed-id.stream.ndjson"
    stream.write_text("event\n", encoding="utf-8")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; print('held', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        (root / "closed-id.stream.lock").write_text(
            json.dumps({"pid": holder.pid, "created_at": datetime.now(timezone.utc).isoformat()}) + "\n",
            encoding="utf-8",
        )
        _touch_old(record)
        _touch_old(stream)
        source = archive_agent_storage.ArchiveSource(
            "acpx",
            root,
            "sessions",
            "acpx_sessions",
            authority_root=root,
            allows_external_root=True,
        )
        grouped, _protected = archive_agent_storage.collect_source_units(
            source,
            datetime.now(timezone.utc) - timedelta(days=7),
        )
        units = [unit for values in grouped.values() for unit in values]
        _install_identity_zstd(monkeypatch)

        result = archive_agent_storage.archive_month(units, tmp_path / "archive" / "acpx.tar.zst", apply=True)
    finally:
        holder.kill()
        holder.wait()

    assert record.exists()
    assert stream.exists()
    assert result["removed"] == 0
    assert result["applied"] is False
    assert any(entry["reason"] == "lifecycle lock unavailable" for entry in result["retained"])


def test_kimi_sources_stay_report_only_when_apply_is_disabled(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    (ops / "kimi-home" / "sessions").mkdir(parents=True)
    sources, _purges = archive_agent_storage.agent_storage_policy(ops)
    kimi = [source for source in sources if source.source_id == "kimi-home"]

    assert kimi
    assert all(source.apply_enabled is False for source in kimi)
    assert all(source.protection_reason for source in kimi)

    grouped, protected = archive_agent_storage.collect_source_units(
        kimi[0],
        datetime.now(timezone.utc) - timedelta(days=7),
    )
    assert grouped == {}
    assert any("not yet known" in entry["reason"] for entry in protected)


def _session_search_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE session_docs (session_id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE session_docs_fts USING fts5("
            "title, content, content='session_docs', content_rowid='rowid')"
        )
        connection.execute(
            "CREATE TRIGGER session_docs_ai AFTER INSERT ON session_docs BEGIN "
            "INSERT INTO session_docs_fts(rowid, title, content) "
            "VALUES (new.rowid, new.title, new.content); END"
        )
        connection.execute(
            "CREATE TRIGGER session_docs_ad AFTER DELETE ON session_docs BEGIN "
            "INSERT INTO session_docs_fts(session_docs_fts, rowid, title, content) "
            "VALUES ('delete', old.rowid, old.title, old.content); END"
        )
        connection.executemany(
            "INSERT INTO session_docs VALUES (?, ?, ?)",
            [("old", "obsolete", "old material"), ("live", "current", "live material")],
        )


def test_session_docs_vacuum_journal_replays_after_interrupted_vacuum(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "sessions" / "session_search.sqlite"
    _session_search_db(db_path)
    journal = db_path.with_name("session_search.sqlite.vacuum-journal")

    def fail_vacuum(_path: Path, **_kwargs) -> None:
        raise sqlite3.OperationalError("interrupted VACUUM")

    monkeypatch.setattr(archive_agent_storage, "_vacuum_sqlite", fail_vacuum)
    first = archive_agent_storage._reconcile_session_search(db_path, {"old"}, apply=True)

    assert first["delete_committed"] is True
    assert first["vacuumed"] is False
    assert first.get("error")
    assert "VACUUM" in str(first["error"])
    assert journal.is_file()
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "agent_storage_session_docs_vacuum.v1"
    assert "old" in payload["identities"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT session_id FROM session_docs").fetchall() == [("live",)]

    vacuums: list[Path] = []

    def track_vacuum(path: Path, **kwargs) -> None:
        vacuums.append(path)
        with sqlite3.connect(path, isolation_level=None, timeout=5) as connection:
            connection.execute("VACUUM")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    monkeypatch.setattr(archive_agent_storage, "_vacuum_sqlite", track_vacuum)
    second = archive_agent_storage._reconcile_session_search(db_path, {"old"}, apply=True)

    assert second["vacuumed"] is True
    assert second.get("error") is None
    assert vacuums == [db_path]
    assert not journal.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT session_id FROM session_docs").fetchall() == [("live",)]


def test_session_docs_vacuum_journal_symlink_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions" / "session_search.sqlite"
    _session_search_db(db_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    journal = db_path.with_name("session_search.sqlite.vacuum-journal")
    journal.symlink_to(outside)

    result = archive_agent_storage._reconcile_session_search(db_path, {"old"}, apply=True)

    assert result["applied"] is False
    assert result["delete_committed"] is False
    assert result["vacuumed"] is False
    assert "symlink" in result["error"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM session_docs").fetchone() == (2,)


def test_failed_published_verification_restores_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    destination = tmp_path / "archive" / "sessions.tar.zst"
    _install_identity_zstd(monkeypatch)
    real_publish = archive_agent_storage._publish_archive

    def corrupt_publish(staged: Path, dest: Path) -> Path:
        published = real_publish(staged, dest)
        published.unlink()
        published.write_bytes(b"not-a-zstd-archive")
        return published

    monkeypatch.setattr(archive_agent_storage, "_publish_archive", corrupt_publish)

    with pytest.raises(Exception, match="archive|zstd|tar|verif"):
        archive_agent_storage.archive_month([source], destination, apply=True)

    assert source.exists()
    assert source.read_text(encoding="utf-8") == "transcript"


def test_archive_fsyncs_published_artifact_before_deleting_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    destination = tmp_path / "archive" / "sessions.tar.zst"
    _install_identity_zstd(monkeypatch)
    synced: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        archive_agent_storage,
        "_fsync_file",
        lambda path: synced.append(("file", path)),
    )
    monkeypatch.setattr(
        archive_agent_storage,
        "_fsync_dir",
        lambda path: synced.append(("dir", path)),
    )

    result = archive_agent_storage.archive_month([source], destination, apply=True)

    published = Path(result["archive"])
    assert ("file", published) in synced
    assert ("dir", published.parent) in synced
    assert not source.exists()


def test_prompt_history_fail_closed_preserves_concurrent_append(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "grok-home"
    workspace = home / "sessions" / "%2Frepo"
    workspace.mkdir(parents=True)
    (home / "active_sessions.json").write_text("[]", encoding="utf-8")
    (home / "active_sessions.lock").touch()
    history = workspace / "prompt_history.jsonl"
    original = (
        json.dumps({"session_id": "old", "prompt": "old"})
        + "\n"
        + json.dumps({"session_id": "live", "prompt": "live"})
        + "\n"
    )
    history.write_text(original, encoding="utf-8")
    extra = json.dumps({"session_id": "live", "prompt": "concurrent"}) + "\n"
    real_chmod = os.chmod

    def chmod_and_append(path, mode):
        if Path(path).parent == history.parent:
            history.write_bytes(history.read_bytes() + extra.encode("utf-8"))
        real_chmod(path, mode)

    monkeypatch.setattr(archive_agent_storage.os, "chmod", chmod_and_append)

    result = archive_agent_storage.reconcile_grok_session_metadata(home, {"old"}, apply=True)

    assert result.get("applied") is False
    assert "prompt history" in str(result.get("error", ""))
    text = history.read_text(encoding="utf-8")
    assert "concurrent" in text
    assert json.dumps({"session_id": "old", "prompt": "old"}) in text


def _patch_main_roots(monkeypatch, tmp_path: Path, *, sessions: Path | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    monkeypatch.setattr(archive_agent_storage, "REPO_ROOT", repo)
    monkeypatch.setattr(archive_agent_storage, "CODEX_HOME", tmp_path / "codex-home")
    monkeypatch.setattr(archive_agent_storage, "ACPX_SESSIONS", sessions or tmp_path / "no-sessions")
    monkeypatch.setattr(archive_agent_storage, "ARCHIVE_ROOT", tmp_path / "archives")
    monkeypatch.setattr(
        archive_agent_storage,
        "_extract_llm_usage_before_purge",
        lambda: {"applied": True, "appended": 0},
    )
    return repo


def test_apply_fails_visibly_when_retention_lock_is_held(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _patch_main_roots(monkeypatch, tmp_path)
    lock_path = repo / "state" / "retention" / "apply.lock"
    lock_path.parent.mkdir(parents=True, mode=0o700)
    lock_path.touch()
    extracts: list[str] = []
    monkeypatch.setattr(
        archive_agent_storage,
        "_extract_llm_usage_before_purge",
        lambda: extracts.append("extract") or {"applied": True, "appended": 0},
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, time\n"
            f"handle = open({str(lock_path)!r}, 'rb')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        assert archive_agent_storage.main(["--only", "logs", "--apply"]) == 1
    finally:
        holder.kill()
        holder.wait()

    report = json.loads(capsys.readouterr().out)
    assert extracts == []
    assert report["status"] == "blocked"
    assert "lock" in report["error"]
    assert report["retention_lock"]["held_by_other"] is True


def test_dry_run_reports_lock_state_without_holding_or_mutating(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _patch_main_roots(monkeypatch, tmp_path)
    lock_path = repo / "state" / "retention" / "apply.lock"
    lock_path.parent.mkdir(parents=True, mode=0o700)
    lock_path.touch()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, time\n"
            f"handle = open({str(lock_path)!r}, 'rb')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        assert archive_agent_storage.main(["--only", "logs"]) == 0
    finally:
        holder.kill()
        holder.wait()

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["retention_lock"]["held_by_other"] is True
    assert report["retention_lock"].get("acquired") is not True


def test_failed_reconciliation_replays_from_journal_with_zero_candidates(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    record = sessions / "closed-id.json"
    record.write_text(json.dumps({"closed": True, "acpx_record_id": "closed-id"}), encoding="utf-8")
    _touch_old(record)
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    _install_identity_zstd(monkeypatch)
    calls: list[set[str]] = []

    def failing_reconcile(root, identities, *, apply):
        calls.append(set(identities))
        return {
            "home": str(root.parent),
            "kind": "acpx_session_index",
            "identities": len(identities),
            "applied": False,
            "error": "simulated reconciliation crash",
        }

    monkeypatch.setattr(archive_agent_storage, "reconcile_acpx_session_index", failing_reconcile)

    assert archive_agent_storage.main(["--only", "sessions", "--apply"]) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "partial_failure"
    assert not record.exists()
    journal_path = tmp_path / "repo" / "state" / "retention" / "pending-reconciliation.json"
    assert journal_path.is_file()
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "agent_storage_pending_reconciliation.v1"
    assert payload["last_error"]
    assert payload["entries"][0]["identities"] == ["closed-id"]
    assert calls == [{"closed-id"}]

    def succeeding_reconcile(root, identities, *, apply):
        calls.append(set(identities))
        assert apply is True
        return {
            "home": str(root.parent),
            "kind": "acpx_session_index",
            "identities": len(identities),
            "applied": True,
            "records": 0,
        }

    monkeypatch.setattr(archive_agent_storage, "reconcile_acpx_session_index", succeeding_reconcile)

    assert archive_agent_storage.main(["--only", "sessions", "--apply"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "ok"
    assert calls[1] == {"closed-id"}
    assert not any(entry.get("applied") for entry in second["archives"])
    assert not journal_path.exists() or json.loads(journal_path.read_text(encoding="utf-8")).get("entries") == []


def _archive_unit_for(path: Path, *, identity: str | None = None, closure_record: Path | None = None):
    snapshot = archive_agent_storage._tree_snapshot(path)
    assert snapshot is not None
    size, recorded_at, fingerprint = snapshot
    return archive_agent_storage.ArchiveUnit(
        path=path,
        arcname=f"acpx/sessions/{path.name}",
        recorded_at=recorded_at,
        size=size,
        identity=identity,
        fingerprint=fingerprint,
        guard_root=path.parent,
        closure_record=closure_record,
    )


def _write_closed_acpx(root: Path, identity: str) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    record = root / f"{identity}.json"
    record.write_text(json.dumps({"closed": True, "acpx_record_id": identity}), encoding="utf-8")
    stream = root / f"{identity}.stream.ndjson"
    stream.write_text(f"{identity}-event\n", encoding="utf-8")
    _touch_old(record)
    _touch_old(stream)
    return record, stream


def _intent_paths(guard_root: Path) -> list[Path]:
    intent_dir = guard_root / ".retention-quarantine" / ".intent"
    if not intent_dir.exists():
        return []
    return sorted(path for path in intent_dir.glob("*.json") if not path.name.startswith("."))


def _write_real_published_archive(published: Path, units) -> str:
    published.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(published, "w") as tar:
        for unit in units:
            tar.add(unit.path, arcname=unit.arcname, recursive=True)
        members = [
            {
                "arcname": unit.arcname,
                "identity": unit.identity,
                "recorded_at": unit.recorded_at.isoformat(),
                "size": unit.size,
                "fingerprint": unit.fingerprint,
            }
            for unit in units
        ]
        payload = json.dumps(
            {"schema_version": "agent_storage_archive.v1", "members": members},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        info = tarfile.TarInfo("_archive/manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return archive_agent_storage._sha256_file(published)


def _stub_no_new_archive(monkeypatch) -> None:
    monkeypatch.setattr(
        archive_agent_storage,
        "archive_month",
        lambda *_args, **_kwargs: {
            "files": 0,
            "logical_units": 0,
            "raw_size": 0,
            "applied": False,
            "removed": 0,
            "retained": [],
            "removed_identities": [],
        },
    )


def test_intent_is_fsynced_before_the_first_live_to_quarantine_rename(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    seen_intents: list[int] = []
    real_rename = os.rename

    def rename_and_count(src, dst, *args):
        seen_intents.append(len(_intent_paths(source.parent)))
        return real_rename(src, dst, *args)

    monkeypatch.setattr(archive_agent_storage.os, "rename", rename_and_count)
    _install_identity_zstd(monkeypatch)
    _patch_main_roots(monkeypatch, tmp_path)

    archive_agent_storage.archive_month([source], tmp_path / "archive" / "sessions.tar.zst", apply=True)

    assert seen_intents
    assert all(count >= 1 for count in seen_intents)


def test_successful_archive_removes_intent_and_empty_quarantine_tree(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    _install_identity_zstd(monkeypatch)
    _patch_main_roots(monkeypatch, tmp_path)

    result = archive_agent_storage.archive_month([source], tmp_path / "archive" / "sessions.tar.zst", apply=True)

    assert result["applied"] is True
    assert not source.exists()
    assert _intent_paths(source.parent) == []
    quarantine = source.parent / ".retention-quarantine"
    assert not quarantine.exists()


def test_keyboardinterrupt_after_quarantine_restores_live_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    _install_identity_zstd(monkeypatch)
    _patch_main_roots(monkeypatch, tmp_path)

    def boom(_staged, _destination):
        raise KeyboardInterrupt()

    monkeypatch.setattr(archive_agent_storage, "_publish_archive", boom)

    with pytest.raises(KeyboardInterrupt):
        archive_agent_storage.archive_month([source], tmp_path / "archive" / "sessions.tar.zst", apply=True)

    assert source.exists()
    assert source.read_text(encoding="utf-8") == "transcript"
    assert _intent_paths(source.parent) == []
    assert not (source.parent / ".retention-quarantine").exists()


def test_crash_before_journal_replays_restore_and_does_not_reconcile_live(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    sessions = tmp_path / "sessions"
    record, stream = _write_closed_acpx(sessions, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    _install_identity_zstd(monkeypatch)
    units = [
        _archive_unit_for(record, identity="closed-id", closure_record=record),
        _archive_unit_for(stream, identity="closed-id", closure_record=record),
    ]
    intent = QuarantineTransactionIntent.from_archive_units(
        units,
        transaction_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        source_id="acpx",
        destination=tmp_path / "archives" / "acpx-sessions-2026-01.tar.zst",
    )
    archive_agent_storage.save_quarantine_intent(intent)
    for unit in intent.units:
        moved = archive_agent_storage._quarantine_unit(unit.as_archive_unit(quarantined=False))
        assert moved.path == unit.quarantine_path
    assert not record.exists()
    assert not stream.exists()
    journal_path = tmp_path / "repo" / "state" / "retention" / "pending-reconciliation.json"
    assert not journal_path.exists()

    reconciled: list[set[str]] = []

    def spy_reconcile(root, identities, *, apply):
        reconciled.append(set(identities))
        return {
            "home": str(root.parent),
            "kind": "acpx_session_index",
            "identities": len(identities),
            "applied": True,
            "records": 0,
        }

    monkeypatch.setattr(archive_agent_storage, "reconcile_acpx_session_index", spy_reconcile)
    monkeypatch.setattr(
        archive_agent_storage,
        "archive_month",
        lambda *args, **kwargs: {
            "files": 0,
            "logical_units": 0,
            "raw_size": 0,
            "applied": False,
            "removed": 0,
            "retained": [],
            "removed_identities": [],
        },
    )

    assert archive_agent_storage.main(["--only", "sessions", "--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert record.exists()
    assert stream.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["closed"] is True
    assert stream.read_text(encoding="utf-8") == "closed-id-event\n"
    assert _intent_paths(sessions) == []
    assert not journal_path.exists()
    assert report["status"] == "ok"
    assert all("closed-id" not in identities for identities in reconciled)


def test_partial_rename_replays_restore_of_moved_units_only(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / "sessions"
    record, stream = _write_closed_acpx(sessions, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    units = [
        _archive_unit_for(record, identity="closed-id", closure_record=record),
        _archive_unit_for(stream, identity="closed-id", closure_record=record),
    ]
    intent = QuarantineTransactionIntent.from_archive_units(
        units,
        transaction_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        source_id="acpx",
        destination=tmp_path / "archives" / "acpx.tar.zst",
    )
    archive_agent_storage.save_quarantine_intent(intent)
    archive_agent_storage._quarantine_unit(intent.units[0].as_archive_unit(quarantined=False))
    assert not record.exists()
    assert stream.exists()

    reports = archive_agent_storage.recover_leftover_quarantine_transactions(
        [sessions],
        PendingReconciliationJournal(),
    )

    assert reports[0]["action"] == "restore"
    assert record.exists()
    assert stream.exists()
    assert record.read_text(encoding="utf-8")
    assert stream.read_text(encoding="utf-8") == "closed-id-event\n"
    assert _intent_paths(sessions) == []


def test_crash_after_journal_with_partial_delete_finalizes_remaining_quarantine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions = tmp_path / "sessions"
    record, stream = _write_closed_acpx(sessions, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    _install_identity_zstd(monkeypatch)
    units = [
        _archive_unit_for(record, identity="closed-id", closure_record=record),
        _archive_unit_for(stream, identity="closed-id", closure_record=record),
    ]
    published = tmp_path / "archives" / "acpx-sessions-2026-01.tar.zst"
    intent = QuarantineTransactionIntent.from_archive_units(
        units,
        transaction_id="cccccccccccccccccccccccccccccccc",
        source_id="acpx",
        destination=published,
    )
    archive_agent_storage.save_quarantine_intent(intent)
    moved = [archive_agent_storage._quarantine_unit(unit.as_archive_unit(quarantined=False)) for unit in intent.units]
    digest = _write_real_published_archive(published, moved)
    intent = intent.with_verified_archive(published, digest)
    archive_agent_storage.save_quarantine_intent(intent)
    archive_agent_storage.save_pending_journal(PendingReconciliationJournal().merge("acpx", {"closed-id"}))
    archive_agent_storage._remove_unit(moved[0])
    assert not record.exists()
    assert not intent.units[0].quarantine_path.exists()
    assert intent.units[1].quarantine_path.exists()

    reports = archive_agent_storage.recover_leftover_quarantine_transactions(
        [sessions],
        archive_agent_storage.load_pending_journal(),
    )

    assert reports[0]["action"] == "finalize"
    assert not record.exists()
    assert not stream.exists()
    assert not intent.units[0].quarantine_path.exists()
    assert not intent.units[1].quarantine_path.exists()
    assert _intent_paths(sessions) == []
    journal = archive_agent_storage.load_pending_journal()
    assert journal.identities_for("acpx") == frozenset({"closed-id"})


def test_corrupt_intent_blocks_apply_without_deleting_or_reconciling(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    sessions = tmp_path / "sessions"
    record, _stream = _write_closed_acpx(sessions, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    _install_identity_zstd(monkeypatch)
    intent_dir = sessions / ".retention-quarantine" / ".intent"
    intent_dir.mkdir(parents=True, mode=0o700)
    (intent_dir / "dddddddddddddddddddddddddddddddd.json").write_text("{not-json", encoding="utf-8")
    reconciled: list[set[str]] = []
    monkeypatch.setattr(
        archive_agent_storage,
        "reconcile_acpx_session_index",
        lambda root, identities, *, apply: reconciled.append(set(identities)) or {"applied": True, "identities": 0},
    )

    assert archive_agent_storage.main(["--only", "sessions", "--apply"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert "intent" in report["error"]
    assert record.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["closed"] is True
    assert reconciled == []


def _acpx_units_for(record: Path, stream: Path, identity: str):
    return [
        _archive_unit_for(record, identity=identity, closure_record=record),
        _archive_unit_for(stream, identity=identity, closure_record=record),
    ]


def _quarantine_rel(path: Path, guard: Path) -> Path:
    return guard / ".retention-quarantine" / path.relative_to(guard)


def test_exception_before_journal_replace_leaves_replay_to_restore_without_reconciling(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    sessions = tmp_path / "sessions"
    record, stream = _write_closed_acpx(sessions, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    _install_identity_zstd(monkeypatch)
    units = _acpx_units_for(record, stream, "closed-id")
    destination = tmp_path / "archives" / "acpx-sessions-2026-01.tar.zst"
    real_replace = archive_agent_storage.os.replace

    def fail_before_journal_replace(src, dst, *args):
        if Path(dst).name == "pending-reconciliation.json":
            raise KeyboardInterrupt("injected before journal replace")
        return real_replace(src, dst, *args)

    monkeypatch.setattr(archive_agent_storage.os, "replace", fail_before_journal_replace)

    with pytest.raises(KeyboardInterrupt, match="journal replace"):
        archive_agent_storage.archive_month(
            units,
            destination,
            apply=True,
            source_id="acpx",
            commit_identities=lambda identities: archive_agent_storage._commit_source_identities("acpx", identities),
        )

    monkeypatch.setattr(archive_agent_storage.os, "replace", real_replace)
    journal_path = tmp_path / "repo" / "state" / "retention" / "pending-reconciliation.json"
    assert not record.exists()
    assert not stream.exists()
    assert _quarantine_rel(record, sessions).exists()
    assert _intent_paths(sessions)
    assert not journal_path.exists()

    reconciled: list[set[str]] = []

    def spy_reconcile(root, identities, *, apply):
        reconciled.append(set(identities))
        assert record.exists()
        return {
            "home": str(root.parent),
            "kind": "acpx_session_index",
            "identities": len(identities),
            "applied": True,
            "records": 0,
        }

    monkeypatch.setattr(archive_agent_storage, "reconcile_acpx_session_index", spy_reconcile)
    _stub_no_new_archive(monkeypatch)

    assert archive_agent_storage.main(["--only", "sessions", "--apply"]) == 0
    json.loads(capsys.readouterr().out)
    assert record.exists()
    assert stream.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["closed"] is True
    assert _intent_paths(sessions) == []
    assert not journal_path.exists()
    assert all("closed-id" not in identities for identities in reconciled)


def test_exception_after_journal_replace_fsync_leaves_replay_to_finalize_without_live_reconcile(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    sessions = tmp_path / "sessions"
    record, stream = _write_closed_acpx(sessions, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    _install_identity_zstd(monkeypatch)
    units = _acpx_units_for(record, stream, "closed-id")
    destination = tmp_path / "archives" / "acpx-sessions-2026-01.tar.zst"
    real_fsync = archive_agent_storage._fsync_file

    def fail_after_journal_fsync(path):
        real_fsync(path)
        if Path(path).name == "pending-reconciliation.json":
            raise OSError("injected after journal replace/fsync")

    monkeypatch.setattr(archive_agent_storage, "_fsync_file", fail_after_journal_fsync)

    with pytest.raises(OSError, match="journal replace/fsync"):
        archive_agent_storage.archive_month(
            units,
            destination,
            apply=True,
            source_id="acpx",
            commit_identities=lambda identities: archive_agent_storage._commit_source_identities("acpx", identities),
        )

    monkeypatch.setattr(archive_agent_storage, "_fsync_file", real_fsync)
    journal_path = tmp_path / "repo" / "state" / "retention" / "pending-reconciliation.json"
    assert not record.exists()
    assert not stream.exists()
    assert _quarantine_rel(record, sessions).exists()
    assert _intent_paths(sessions)
    assert journal_path.is_file()
    assert json.loads(journal_path.read_text(encoding="utf-8"))["entries"][0]["identities"] == ["closed-id"]

    def spy_reconcile(root, identities, *, apply):
        assert not record.exists(), "refusing to reconcile metadata for a live session"
        return {
            "home": str(root.parent),
            "kind": "acpx_session_index",
            "identities": len(identities),
            "applied": True,
            "records": 0,
        }

    monkeypatch.setattr(archive_agent_storage, "reconcile_acpx_session_index", spy_reconcile)
    _stub_no_new_archive(monkeypatch)

    assert archive_agent_storage.main(["--only", "sessions", "--apply"]) == 0
    json.loads(capsys.readouterr().out)
    assert not record.exists()
    assert not stream.exists()
    assert _intent_paths(sessions) == []
    journal = archive_agent_storage.load_pending_journal()
    assert journal.identities_for("acpx") == frozenset()


def test_restore_refuses_symlink_ancestor_and_keeps_quarantine(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / "sessions"
    nested = sessions / "nested"
    record, stream = _write_closed_acpx(nested, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    units = []
    for path in (record, stream):
        snapshot = archive_agent_storage._tree_snapshot(path)
        assert snapshot is not None
        size, recorded_at, fingerprint = snapshot
        units.append(
            archive_agent_storage.ArchiveUnit(
                path=path,
                arcname=f"acpx/sessions/nested/{path.name}",
                recorded_at=recorded_at,
                size=size,
                identity="closed-id",
                fingerprint=fingerprint,
                guard_root=sessions,
                closure_record=record,
            )
        )
    intent = QuarantineTransactionIntent.from_archive_units(
        units,
        transaction_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        source_id="acpx",
        destination=tmp_path / "archives" / "acpx.tar.zst",
    )
    archive_agent_storage.save_quarantine_intent(intent)
    for unit in intent.units:
        archive_agent_storage._quarantine_unit(unit.as_archive_unit(quarantined=False))
    nested.rmdir()
    evil = tmp_path / "evil"
    evil.mkdir()
    nested.symlink_to(evil)

    with pytest.raises((archive_agent_storage.QuarantineRecoveryError, RuntimeError), match="symlink|ancestor"):
        archive_agent_storage.recover_leftover_quarantine_transactions(
            [sessions],
            PendingReconciliationJournal(),
        )

    assert _quarantine_rel(record, sessions).exists()
    assert _quarantine_rel(stream, sessions).exists()
    assert _intent_paths(sessions)
    assert list(evil.iterdir()) == []
    assert not (evil / "closed-id.json").exists()


def test_quarantine_refuses_symlink_ancestor_under_quarantine_and_keeps_live_source(
    tmp_path: Path, monkeypatch
) -> None:
    sessions = tmp_path / "sessions"
    nested = sessions / "nested"
    record, stream = _write_closed_acpx(nested, "closed-id")
    live_record = record.read_text(encoding="utf-8")
    live_stream = stream.read_text(encoding="utf-8")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    units = []
    for path in (record, stream):
        snapshot = archive_agent_storage._tree_snapshot(path)
        assert snapshot is not None
        size, recorded_at, fingerprint = snapshot
        units.append(
            archive_agent_storage.ArchiveUnit(
                path=path,
                arcname=f"acpx/sessions/nested/{path.name}",
                recorded_at=recorded_at,
                size=size,
                identity="closed-id",
                fingerprint=fingerprint,
                guard_root=sessions,
                closure_record=record,
            )
        )
    intent = QuarantineTransactionIntent.from_archive_units(
        units,
        transaction_id="a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2",
        source_id="acpx",
        destination=tmp_path / "archives" / "acpx.tar.zst",
    )
    archive_agent_storage.save_quarantine_intent(intent)
    evil = tmp_path / "evil"
    evil.mkdir()
    (sessions / ".retention-quarantine" / "nested").symlink_to(evil)

    with pytest.raises((archive_agent_storage.QuarantineRecoveryError, RuntimeError), match="symlink|ancestor"):
        archive_agent_storage._quarantine_unit(intent.units[0].as_archive_unit(quarantined=False))

    assert record.exists()
    assert stream.exists()
    assert not archive_agent_storage._is_symlink(nested)
    assert json.loads(record.read_text(encoding="utf-8"))["closed"] is True
    assert record.read_text(encoding="utf-8") == live_record
    assert stream.read_text(encoding="utf-8") == live_stream
    assert _intent_paths(sessions)
    assert archive_agent_storage._is_symlink(sessions / ".retention-quarantine" / "nested")
    assert list(evil.iterdir()) == []
    assert not (evil / "closed-id.json").exists()
    assert not (evil / "closed-id.stream.ndjson").exists()


def _quarantine_nested_closed_acpx(tmp_path: Path, monkeypatch, *, transaction_id: str):
    sessions = tmp_path / "sessions"
    nested = sessions / "nested"
    record, stream = _write_closed_acpx(nested, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    units = []
    for path in (record, stream):
        snapshot = archive_agent_storage._tree_snapshot(path)
        assert snapshot is not None
        size, recorded_at, fingerprint = snapshot
        units.append(
            archive_agent_storage.ArchiveUnit(
                path=path,
                arcname=f"acpx/sessions/nested/{path.name}",
                recorded_at=recorded_at,
                size=size,
                identity="closed-id",
                fingerprint=fingerprint,
                guard_root=sessions,
                closure_record=record,
            )
        )
    intent = QuarantineTransactionIntent.from_archive_units(
        units,
        transaction_id=transaction_id,
        source_id="acpx",
        destination=tmp_path / "archives" / "acpx.tar.zst",
    )
    archive_agent_storage.save_quarantine_intent(intent)
    for unit in intent.units:
        archive_agent_storage._quarantine_unit(unit.as_archive_unit(quarantined=False))
    return sessions, nested, record, stream, intent


def test_restore_refuses_missing_ancestor_and_keeps_quarantine(tmp_path: Path, monkeypatch) -> None:
    sessions, nested, record, stream, _intent = _quarantine_nested_closed_acpx(
        tmp_path,
        monkeypatch,
        transaction_id="78787878787878787878787878787878",
    )
    nested.rmdir()

    with pytest.raises(
        (archive_agent_storage.QuarantineRecoveryError, RuntimeError),
        match="missing|directory",
    ):
        archive_agent_storage.recover_leftover_quarantine_transactions(
            [sessions],
            PendingReconciliationJournal(),
        )

    assert not nested.exists()
    assert not record.exists()
    assert not stream.exists()
    assert _quarantine_rel(record, sessions).exists()
    assert _quarantine_rel(stream, sessions).exists()
    assert _intent_paths(sessions)


def test_restore_succeeds_when_original_ancestors_exist(tmp_path: Path, monkeypatch) -> None:
    sessions, nested, record, stream, _intent = _quarantine_nested_closed_acpx(
        tmp_path,
        monkeypatch,
        transaction_id="9a9a9a9a9a9a9a9a9a9a9a9a9a9a9a9a",
    )

    reports = archive_agent_storage.recover_leftover_quarantine_transactions(
        [sessions],
        PendingReconciliationJournal(),
    )

    assert reports[0]["action"] == "restore"
    assert nested.is_dir()
    assert not archive_agent_storage._is_symlink(nested)
    assert record.exists()
    assert stream.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["closed"] is True
    assert stream.read_text(encoding="utf-8") == "closed-id-event\n"
    assert _intent_paths(sessions) == []
    assert not _quarantine_rel(record, sessions).exists()
    assert not _quarantine_rel(stream, sessions).exists()


def test_incomplete_restore_refuses_live_conflict_and_keeps_intent(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / "sessions"
    record, stream = _write_closed_acpx(sessions, "closed-id")
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    units = _acpx_units_for(record, stream, "closed-id")
    intent = QuarantineTransactionIntent.from_archive_units(
        units,
        transaction_id="12121212121212121212121212121212",
        source_id="acpx",
        destination=tmp_path / "archives" / "acpx.tar.zst",
    )
    archive_agent_storage.save_quarantine_intent(intent)
    for unit in intent.units:
        archive_agent_storage._quarantine_unit(unit.as_archive_unit(quarantined=False))
    stream.write_text("conflict\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="already exists"):
        archive_agent_storage._restore_quarantined(
            [intent.units[1].as_archive_unit(), intent.units[0].as_archive_unit()]
        )

    assert record.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["closed"] is True
    assert _intent_paths(sessions)
    assert _quarantine_rel(stream, sessions).exists()
    assert stream.read_text(encoding="utf-8") == "conflict\n"


def _leftover_verified_acpx(tmp_path: Path, monkeypatch, identity: str = "closed-id"):
    sessions = tmp_path / "sessions"
    record, stream = _write_closed_acpx(sessions, identity)
    _patch_main_roots(monkeypatch, tmp_path, sessions=sessions)
    _install_identity_zstd(monkeypatch)
    units = _acpx_units_for(record, stream, identity)
    published = tmp_path / "archives" / "acpx-sessions-2026-01.tar.zst"
    intent = QuarantineTransactionIntent.from_archive_units(
        units,
        transaction_id="34343434343434343434343434343434",
        source_id="acpx",
        destination=published,
    )
    archive_agent_storage.save_quarantine_intent(intent)
    moved = [archive_agent_storage._quarantine_unit(unit.as_archive_unit(quarantined=False)) for unit in intent.units]
    digest = _write_real_published_archive(published, moved)
    intent = intent.with_verified_archive(published, digest)
    archive_agent_storage.save_quarantine_intent(intent)
    archive_agent_storage.save_pending_journal(PendingReconciliationJournal().merge("acpx", {identity}))
    return sessions, record, stream, published, intent, moved


def test_truncated_published_archive_does_not_delete_journaled_quarantine(tmp_path: Path, monkeypatch) -> None:
    sessions, record, stream, published, intent, moved = _leftover_verified_acpx(tmp_path, monkeypatch)
    published.write_bytes(published.read_bytes()[:20])

    with pytest.raises(archive_agent_storage.QuarantineRecoveryError, match="journaled|archive"):
        archive_agent_storage.recover_leftover_quarantine_transactions(
            [sessions],
            archive_agent_storage.load_pending_journal(),
        )

    assert not record.exists()
    assert not stream.exists()
    assert moved[0].path.exists()
    assert moved[1].path.exists()
    assert _intent_paths(sessions)
    assert archive_agent_storage.load_pending_journal().identities_for("acpx") == frozenset({"closed-id"})


def test_replaced_published_archive_does_not_delete_journaled_quarantine(tmp_path: Path, monkeypatch) -> None:
    sessions, record, stream, published, intent, moved = _leftover_verified_acpx(tmp_path, monkeypatch)
    published.write_bytes(published.read_bytes() + b"\x00tamper")

    with pytest.raises(archive_agent_storage.QuarantineRecoveryError, match="journaled|archive"):
        archive_agent_storage.recover_leftover_quarantine_transactions(
            [sessions],
            archive_agent_storage.load_pending_journal(),
        )

    assert moved[0].path.exists()
    assert moved[1].path.exists()
    assert _intent_paths(sessions)
    assert not record.exists()
    assert not stream.exists()


def test_wrong_path_published_archive_does_not_delete_journaled_quarantine(tmp_path: Path, monkeypatch) -> None:
    sessions, record, stream, published, intent, moved = _leftover_verified_acpx(tmp_path, monkeypatch)
    outside = tmp_path / "outside" / "evil.tar.zst"
    outside.parent.mkdir()
    shutil.copyfile(published, outside)
    tampered = intent.to_payload()
    tampered["published_archive"] = str(outside)
    archive_agent_storage.save_quarantine_intent(QuarantineTransactionIntent.from_payload(tampered))

    with pytest.raises(archive_agent_storage.QuarantineRecoveryError, match="journaled|archive|confined|escape"):
        archive_agent_storage.recover_leftover_quarantine_transactions(
            [sessions],
            archive_agent_storage.load_pending_journal(),
        )

    assert moved[0].path.exists()
    assert moved[1].path.exists()
    assert _intent_paths(sessions)
    assert not record.exists()
    assert not stream.exists()


def test_missing_published_archive_does_not_delete_journaled_quarantine(tmp_path: Path, monkeypatch) -> None:
    sessions, record, stream, published, intent, moved = _leftover_verified_acpx(tmp_path, monkeypatch)
    published.unlink()

    with pytest.raises(archive_agent_storage.QuarantineRecoveryError, match="journaled|archive"):
        archive_agent_storage.recover_leftover_quarantine_transactions(
            [sessions],
            archive_agent_storage.load_pending_journal(),
        )

    assert moved[0].path.exists()
    assert moved[1].path.exists()
    assert _intent_paths(sessions)
    assert not record.exists()
    assert not stream.exists()


def test_identityless_truncated_archive_restores_instead_of_finalizing(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    source = logs / "old.log"
    source.write_text("trace-line\n", encoding="utf-8")
    _patch_main_roots(monkeypatch, tmp_path)
    _install_identity_zstd(monkeypatch)
    unit = _archive_unit_for(source)
    published = tmp_path / "archives" / "grok-home-logs-2026-01.tar.zst"
    intent = QuarantineTransactionIntent.from_archive_units(
        [unit],
        transaction_id="56565656565656565656565656565656",
        destination=published,
    )
    archive_agent_storage.save_quarantine_intent(intent)
    moved = archive_agent_storage._quarantine_unit(intent.units[0].as_archive_unit(quarantined=False))
    digest = _write_real_published_archive(published, [moved])
    intent = intent.with_verified_archive(published, digest)
    archive_agent_storage.save_quarantine_intent(intent)
    published.write_bytes(published.read_bytes()[:12])

    reports = archive_agent_storage.recover_leftover_quarantine_transactions(
        [logs],
        PendingReconciliationJournal(),
    )

    assert reports[0]["action"] == "restore"
    assert source.exists()
    assert source.read_text(encoding="utf-8") == "trace-line\n"
    assert _intent_paths(logs) == []


def test_identityless_crash_after_verified_intent_finalizes_on_replay(tmp_path: Path, monkeypatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    source = logs / "old.log"
    source.write_text("trace-line\n", encoding="utf-8")
    _patch_main_roots(monkeypatch, tmp_path)
    _install_identity_zstd(monkeypatch)
    real_remove = archive_agent_storage._remove_unit

    def fail_remove(_unit):
        raise OSError("injected after verified intent")

    monkeypatch.setattr(archive_agent_storage, "_remove_unit", fail_remove)

    with pytest.raises(OSError, match="verified intent"):
        archive_agent_storage.archive_month(
            [source],
            tmp_path / "archives" / "grok-home-logs-2026-01.tar.zst",
            apply=True,
        )

    monkeypatch.setattr(archive_agent_storage, "_remove_unit", real_remove)
    assert not source.exists()
    assert _intent_paths(logs)
    reports = archive_agent_storage.recover_leftover_quarantine_transactions(
        [logs],
        PendingReconciliationJournal(),
    )
    assert reports[0]["action"] == "finalize"
    assert not source.exists()
    assert _intent_paths(logs) == []


def _swap_directory_for_symlink(path: Path, target: Path) -> None:
    if path.is_symlink() or not path.exists():
        return
    os.rename(path, path.with_name(f"{path.name}.swapped-away"))
    path.symlink_to(target)


def _patch_path_traversal_swap(monkeypatch, victim: Path, outside: Path) -> None:
    """Swap ``victim`` to a symlink just before a path-based directory walk."""

    real_open = os.open
    real_scandir = os.scandir
    real_listdir = os.listdir
    swapped = False

    def victim_path(path_obj: object) -> bool:
        try:
            candidate = Path(os.fsdecode(path_obj))
        except (OSError, TypeError, ValueError):
            return False
        return candidate == victim or candidate.resolve() == victim.resolve()

    def maybe_swap(path_obj: object, *, dir_fd: int | None = None) -> None:
        nonlocal swapped
        if swapped:
            return
        if dir_fd is not None:
            if os.fsdecode(path_obj) != victim.name:
                return
        elif not victim_path(path_obj):
            return
        swapped = True
        _swap_directory_for_symlink(victim, outside)

    def swapping_open(path, flags, *args, dir_fd=None, **kwargs):
        if flags & getattr(os, "O_DIRECTORY", 0):
            maybe_swap(path, dir_fd=dir_fd)
        if dir_fd is None:
            return real_open(path, flags, *args, **kwargs)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    def swapping_scandir(path, *args, **kwargs):
        if not isinstance(path, int):
            maybe_swap(path)
        return real_scandir(path, *args, **kwargs)

    def swapping_listdir(path):
        if not isinstance(path, int):
            maybe_swap(path)
        return real_listdir(path)

    monkeypatch.setattr(os, "open", swapping_open)
    monkeypatch.setattr(os, "scandir", swapping_scandir)
    monkeypatch.setattr(os, "listdir", swapping_listdir)


def test_remove_unit_directory_to_symlink_swap_does_not_touch_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    guard = tmp_path / "sessions"
    victim = guard / "old-session"
    nested = victim / "prompts"
    nested.mkdir(parents=True)
    (nested / "prompt.txt").write_text("delete-me", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "precious.txt"
    canary.write_text("keep", encoding="utf-8")
    unit = _archive_unit_for(victim, identity="old-session")
    _patch_path_traversal_swap(monkeypatch, victim, outside)

    with pytest.raises((OSError, RuntimeError)):
        archive_agent_storage._remove_unit(unit)

    assert canary.exists()
    assert canary.read_text(encoding="utf-8") == "keep"
    assert list(outside.iterdir()) == [canary]


def test_remove_unit_unlinks_child_symlink_without_following_target(tmp_path: Path) -> None:
    guard = tmp_path / "sessions"
    session = guard / "old-session"
    session.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "precious.txt"
    canary.write_text("keep", encoding="utf-8")
    (session / "real.txt").write_text("session", encoding="utf-8")
    unit = _archive_unit_for(session, identity="old-session")
    linked = session / "copied.txt"
    linked.symlink_to(canary)

    archive_agent_storage._remove_unit(unit)

    assert not session.exists()
    assert canary.read_text(encoding="utf-8") == "keep"


def test_remove_unit_nested_directory_swap_does_not_enumerate_symlink_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    guard = tmp_path / "sessions"
    session = guard / "old-session"
    nested = session / "prompts"
    nested.mkdir(parents=True)
    (nested / "prompt.txt").write_text("delete-me", encoding="utf-8")
    (session / "summary.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "precious.txt"
    canary.write_text("keep", encoding="utf-8")
    unit = _archive_unit_for(session, identity="old-session")
    _patch_path_traversal_swap(monkeypatch, nested, outside)

    with pytest.raises((OSError, RuntimeError)):
        archive_agent_storage._remove_unit(unit)

    assert canary.exists()
    assert canary.read_text(encoding="utf-8") == "keep"


def test_reconcile_session_search_refuses_symlinked_sessions_and_keeps_external_sqlite(
    tmp_path: Path,
) -> None:
    home = tmp_path / "grok-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    db_path = sessions / "session_search.sqlite"
    _session_search_db(db_path)
    journal = SessionDocsVacuumJournal(
        sqlite_name="session_search.sqlite",
        identities=("old",),
        created_at=datetime.now(timezone.utc),
    )
    journal_path = db_path.with_name("session_search.sqlite.vacuum-journal")
    journal_path.write_text(json.dumps(journal.to_payload()), encoding="utf-8")
    outside = tmp_path / "outside"
    shutil.move(sessions, outside / "sessions")
    (home / "sessions").symlink_to(outside / "sessions")
    external_db = outside / "sessions" / "session_search.sqlite"

    result = archive_agent_storage._reconcile_session_search(
        home / "sessions" / "session_search.sqlite",
        {"old"},
        apply=True,
    )

    assert result["applied"] is False
    assert result["delete_committed"] is False
    assert result["vacuumed"] is False
    assert "symlink" in str(result.get("error") or "").lower()
    with sqlite3.connect(external_db) as connection:
        assert connection.execute("SELECT session_id FROM session_docs ORDER BY 1").fetchall() == [
            ("live",),
            ("old",),
        ]
    assert (outside / "sessions" / "session_search.sqlite.vacuum-journal").is_file()


def test_reconcile_grok_metadata_refuses_sessions_symlink_and_keeps_external_sqlite(
    tmp_path: Path,
) -> None:
    home = tmp_path / "grok-home"
    sessions = home / "sessions"
    workspace = sessions / "%2Frepo"
    workspace.mkdir(parents=True)
    (home / "active_sessions.json").write_text("[]", encoding="utf-8")
    (home / "active_sessions.lock").touch()
    _session_search_db(sessions / "session_search.sqlite")
    history = workspace / "prompt_history.jsonl"
    history.write_text(json.dumps({"session_id": "old", "prompt": "old"}) + "\n", encoding="utf-8")
    outside = tmp_path / "outside"
    shutil.move(sessions, outside / "sessions")
    (home / "sessions").symlink_to(outside / "sessions")
    external_db = outside / "sessions" / "session_search.sqlite"
    external_history = outside / "sessions" / "%2Frepo" / "prompt_history.jsonl"

    result = archive_agent_storage.reconcile_grok_session_metadata(home, {"old"}, apply=True)

    assert result["applied"] is False
    search = result.get("session_search") or {}
    error = str(result.get("error") or search.get("error") or "")
    assert "symlink" in error.lower()
    with sqlite3.connect(external_db) as connection:
        assert connection.execute("SELECT count(*) FROM session_docs").fetchone() == (2,)
    assert json.loads(external_history.read_text(encoding="utf-8"))["session_id"] == "old"


def test_first_archive_publication_refuses_symlink_archive_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    real_root = tmp_path / "real-archives"
    real_root.mkdir()
    linked = tmp_path / "linked-archives"
    linked.symlink_to(real_root)
    _install_identity_zstd(monkeypatch)
    published_through_link: list[Path] = []
    real_link = os.link

    def tracking_link(src, dst, *args, **kwargs):
        published_through_link.append(Path(dst))
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", tracking_link)

    with pytest.raises((OSError, RuntimeError)) as excinfo:
        archive_agent_storage.archive_month([source], linked / "sessions.tar.zst", apply=True)

    assert source.exists()
    assert source.read_text(encoding="utf-8") == "transcript"
    assert published_through_link == []
    assert list(real_root.iterdir()) == []
    assert "symlink" in str(excinfo.value).lower()
