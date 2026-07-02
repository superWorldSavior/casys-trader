"""PID-file ownership primitives for the trader daemon runtime."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_DAEMON_MARKER = "trader.daemon"


def _get_cmdline(pid: int) -> str:
    """Return the process command line for pid, or an empty string on failure."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _is_daemon_pid(pid: int) -> bool:
    """Return True when pid is alive and identified as trader.daemon."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return _DAEMON_MARKER in _get_cmdline(pid)


def claim_pid_file(*, pid_file: Path, pid: int) -> bool:
    """Claim daemon.pid for pid, refusing when a live daemon already owns it."""
    if pid_file.exists():
        try:
            existing = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            existing = None
        if existing is not None and existing != pid and _is_daemon_pid(existing):
            return False
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid), encoding="utf-8")
    return True


def release_pid_file(*, pid_file: Path, pid: int) -> None:
    """Remove daemon.pid only when it still contains pid."""
    try:
        if pid_file.exists() and int(pid_file.read_text(encoding="utf-8").strip()) == pid:
            pid_file.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass
