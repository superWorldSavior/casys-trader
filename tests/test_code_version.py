from pathlib import Path

from trader.support.metadata import code_version


def test_current_code_version_distinguishes_untracked_from_tracked_dirty(
    monkeypatch,
) -> None:
    def fake_git(_repo_root: Path, args: list[str]) -> str | None:
        command = " ".join(args)
        if command == "rev-parse HEAD":
            return "a" * 40
        if command == "rev-parse --short=12 HEAD":
            return "a" * 12
        if command == "rev-parse --abbrev-ref HEAD":
            return "main"
        if command == "show -s --format=%cI HEAD":
            return "2026-08-20T12:00:00+08:00"
        if command == "status --porcelain=v1 --untracked-files=all":
            return "?? prompt_0.txt"
        return None

    monkeypatch.setattr(code_version, "_git", fake_git)

    untracked_only = code_version.current_code_version(Path("."))
    assert untracked_only["git_dirty"] is True
    assert untracked_only["git_tracked_dirty"] is False

    def tracked_git(repo_root: Path, args: list[str]) -> str | None:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return " M trader/runtime/daemon.py\n?? prompt_0.txt"
        return fake_git(repo_root, args)

    monkeypatch.setattr(code_version, "_git", tracked_git)
    tracked = code_version.current_code_version(Path("."))
    assert tracked["git_dirty"] is True
    assert tracked["git_tracked_dirty"] is True


def test_historical_code_version_retrouve_le_commit_avant_un_timestamp(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_git(repo_root: Path, args: list[str]) -> str | None:
        calls.append(args)
        if args[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "main"
        if args[0] == "log":
            return "abcdef1234567890\t2026-06-08T08:00:00+00:00"
        return None

    monkeypatch.setattr(code_version, "_git", fake_git)

    version = code_version.historical_code_version(Path("."), "2026-06-08T10:00:00+00:00")

    assert version["source"] == "git_history"
    assert version["git_commit"] == "abcdef1234567890"
    assert version["git_commit_short"] == "abcdef123456"
    assert version["git_branch"] == "main"
    assert version["git_dirty"] is None
    assert version["inference"]["method"] == "git_log_before_decision_ts"
    assert version["inference"]["decision_ts"] == "2026-06-08T10:00:00+00:00"
    assert any("--before=2026-06-08T10:00:00+00:00" in args for args in calls)
