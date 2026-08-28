from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest

from scripts.render_storage_archive_launchd import (
    PATH_PLACEHOLDER,
    launchd_child_tool_path,
    prepare_storage_archive_logs,
    render_launchd_template,
    write_launchd_plist,
)

_STUB_TEMPLATE = f"work=/ABSOLUTE/PATH/TO/casys-trader\nuv=/opt/homebrew/bin/uv\npath={PATH_PLACEHOLDER}\n"


def test_render_launchd_template_resolves_machine_local_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo with spaces"
    uv = tmp_path / "bin" / "uv"
    rendered = render_launchd_template(
        _STUB_TEMPLATE,
        repo_root=repo,
        uv_binary=uv,
        child_tool_path="/opt/homebrew/bin:/usr/bin:/bin",
    )

    assert rendered == (f"work={repo.resolve()}\nuv={uv.absolute()}\npath=/opt/homebrew/bin:/usr/bin:/bin\n")


def test_write_launchd_plist_is_atomic_and_requires_tracked_contract(tmp_path: Path) -> None:
    template = tmp_path / "source.example"
    output = tmp_path / "generated" / "agent.plist"
    template.write_text(_STUB_TEMPLATE, encoding="utf-8")

    write_launchd_plist(
        template_path=template,
        output_path=output,
        repo_root=tmp_path / "repo",
        uv_binary=tmp_path / "uv",
        child_tool_path="/usr/bin:/bin",
    )

    assert output.read_text(encoding="utf-8").startswith(f"work={(tmp_path / 'repo').resolve()}")
    with pytest.raises(ValueError, match="repository placeholder"):
        render_launchd_template(
            f"uv=/opt/homebrew/bin/uv\npath={PATH_PLACEHOLDER}\n",
            repo_root=tmp_path,
            uv_binary=tmp_path / "uv",
        )
    with pytest.raises(ValueError, match="PATH placeholder"):
        render_launchd_template(
            "work=/ABSOLUTE/PATH/TO/casys-trader\nuv=/opt/homebrew/bin/uv\n",
            repo_root=tmp_path,
            uv_binary=tmp_path / "uv",
        )


def test_tracked_launchd_template_declares_umask_077_and_child_tool_path() -> None:
    template = Path("ops/launchd/ai.casys.trader.storage-archive.plist.example").read_text(encoding="utf-8")

    assert "<key>Umask</key>" in template
    assert "<integer>63</integer>" in template
    assert "<key>EnvironmentVariables</key>" in template
    assert "<key>PATH</key>" in template
    assert PATH_PLACEHOLDER in template
    assert "archive_agent_storage" in template
    assert "archive_world_predictions" not in template


def test_render_launchd_template_xml_escapes_machine_local_values(tmp_path: Path) -> None:
    template = Path("ops/launchd/ai.casys.trader.storage-archive.plist.example").read_text(encoding="utf-8")
    repo = tmp_path / "repo & casys"
    repo.mkdir()
    uv = tmp_path / "bin & brew" / "uv"
    uv.parent.mkdir()
    uv.touch()
    child_path = f"{uv.parent}:/usr/bin:/bin"

    rendered = render_launchd_template(
        template,
        repo_root=repo,
        uv_binary=uv,
        child_tool_path=child_path,
    )
    parsed = plistlib.loads(rendered.encode("utf-8"))

    assert "&amp;" in rendered
    assert parsed["WorkingDirectory"] == str(repo.resolve())
    assert parsed["ProgramArguments"][0] == str(uv.absolute())
    assert parsed["EnvironmentVariables"]["PATH"] == child_path
    assert parsed["Umask"] == 63


def test_launchd_child_tool_path_starts_with_uv_dir_under_minimal_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr("scripts.render_storage_archive_launchd._CHILD_TOOL_FALLBACK_DIRS", ())
    uv = tmp_path / "brew" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.touch()
    (uv.parent / "node").write_text("", encoding="utf-8")
    (uv.parent / "node").chmod(0o755)

    path = launchd_child_tool_path(uv_binary=uv)
    parts = path.split(os.pathsep)

    assert parts[0] == str(uv.parent.absolute())
    assert parts.count(str(uv.parent.absolute())) == 1
    for system_dir in os.defpath.split(os.pathsep):
        if system_dir:
            assert system_dir in parts


def test_prepare_storage_archive_logs_rotates_legacy_multiline_and_secures_mode(
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    log_dir.chmod(0o755)
    legacy = log_dir / "storage-archive.log"
    legacy.write_text('{\n  "status": "ok"\n}\n', encoding="utf-8")
    legacy.chmod(0o644)
    err = log_dir / "storage-archive.err.log"
    err.write_text("trace\n", encoding="utf-8")
    err.chmod(0o644)

    result = prepare_storage_archive_logs(log_dir)

    assert result["rotated"]
    assert legacy.read_text(encoding="utf-8") == ""
    rotated = Path(result["rotated"][0])
    assert rotated.exists()
    assert '{\n  "status": "ok"\n}' in rotated.read_text(encoding="utf-8")
    assert log_dir.stat().st_mode & 0o777 == 0o700
    assert legacy.stat().st_mode & 0o777 == 0o600
    assert err.stat().st_mode & 0o777 == 0o600


def test_prepare_storage_archive_logs_keeps_existing_jsonl(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    path = log_dir / "storage-archive.log"
    path.write_text(json.dumps({"status": "ok"}) + "\n", encoding="utf-8")

    result = prepare_storage_archive_logs(log_dir)

    assert result["rotated"] == []
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "ok"


def test_prepare_storage_archive_logs_refuses_symlinked_log_dir(tmp_path: Path) -> None:
    real = tmp_path / "real-logs"
    real.mkdir()
    linked = tmp_path / "logs"
    linked.symlink_to(real)

    with pytest.raises(RuntimeError, match="symlink"):
        prepare_storage_archive_logs(linked)
    assert list(real.iterdir()) == []
