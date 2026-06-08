from pathlib import Path

from trader import code_version


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
