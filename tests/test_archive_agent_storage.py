from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import archive_agent_storage


def test_archive_month_choisit_un_nom_libre_sans_ecraser(tmp_path, monkeypatch) -> None:
    source = tmp_path / "session.json"
    source.write_text("transcript", encoding="utf-8")
    destination = tmp_path / "archive" / "acpx-sessions-2026-08.tar.zst"
    destination.parent.mkdir()
    destination.write_bytes(b"archive-zero")
    destination.with_name("acpx-sessions-2026-08.1.tar.zst").write_bytes(b"archive-one")

    monkeypatch.setattr(archive_agent_storage, "resolve_zstd", lambda: "zstd")

    def fake_run(argv, *, check):
        assert check is True
        if "-o" in argv:
            staged = Path(argv[argv.index("-o") + 1])
            shutil.copyfile(argv[-1], staged)

    monkeypatch.setattr(archive_agent_storage.subprocess, "run", fake_run)

    result = archive_agent_storage.archive_month([source], destination, apply=True)

    published = destination.with_name("acpx-sessions-2026-08.2.tar.zst")
    assert result["archive"] == str(published)
    assert published.is_file()
    assert destination.read_bytes() == b"archive-zero"
    assert destination.with_name("acpx-sessions-2026-08.1.tar.zst").read_bytes() == b"archive-one"
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
    source.write_text("transcript", encoding="utf-8")
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
    assert source.read_text(encoding="utf-8") == "transcript"
    assert not archive_root.exists()
