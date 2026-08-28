"""Small shared safeguards for local LLM subprocess transports."""

from __future__ import annotations

import os
import signal
import time

DEFAULT_PER_CALL_TIMEOUT_CAP_S = 150


def per_call_timeout_cap_s() -> int:
    raw = os.getenv("CASYS_ACPX_CALL_TIMEOUT_S")
    if raw is None:
        return DEFAULT_PER_CALL_TIMEOUT_CAP_S
    try:
        return max(int(raw), 1)
    except ValueError:
        return DEFAULT_PER_CALL_TIMEOUT_CAP_S


def terminate_process_group(pgid: int, *, grace_s: float = 2.0) -> None:
    if os.name != "posix":
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
