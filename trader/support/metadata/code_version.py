"""Git code-version metadata for decision and audit attribution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_DIRTY_FILES = 80


def unknown_code_version() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "unknown",
        "git_commit": None,
        "git_commit_short": None,
        "git_branch": None,
        "git_dirty": None,
        "git_tracked_dirty": None,
        "git_dirty_files": [],
    }


def _git(repo_root: Path, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def current_code_version(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root)
    commit = _git(root, ["rev-parse", "HEAD"])
    short = _git(root, ["rev-parse", "--short=12", "HEAD"])
    branch = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit_date = _git(root, ["show", "-s", "--format=%cI", "HEAD"])
    status = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if commit is None:
        return unknown_code_version()

    dirty_files = [line for line in (status or "").splitlines() if line.strip()]
    tracked_dirty_files = [
        line for line in dirty_files if not line.startswith("?? ")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "git",
        "git_commit": commit,
        "git_commit_short": short or commit[:12],
        "git_commit_date": commit_date,
        "git_branch": branch,
        "git_dirty": bool(dirty_files),
        "git_tracked_dirty": bool(tracked_dirty_files),
        "git_dirty_files": dirty_files[:MAX_DIRTY_FILES],
    }


def historical_code_version(repo_root: str | Path, decision_ts: str, *, ref: str = "HEAD") -> dict[str, Any]:
    root = Path(repo_root)
    output = _git(
        root,
        [
            "log",
            "--first-parent",
            f"--before={decision_ts}",
            "-1",
            "--format=%H%x09%cI",
            ref,
        ],
    )
    if not output:
        return unknown_code_version()
    commit, _, commit_date = output.partition("\t")
    if not commit:
        return unknown_code_version()
    branch = _git(root, ["rev-parse", "--abbrev-ref", ref]) or ref
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "git_history",
        "git_commit": commit,
        "git_commit_short": commit[:12],
        "git_branch": branch,
        "git_dirty": None,
        "git_tracked_dirty": None,
        "git_dirty_files": [],
        "inference": {
            "method": "git_log_before_decision_ts",
            "decision_ts": decision_ts,
            "commit_date": commit_date or None,
            "ref": ref,
        },
    }
