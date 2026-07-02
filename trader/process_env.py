"""Child-process environment helpers for long-lived runtime processes."""

from __future__ import annotations

import os
from collections.abc import Mapping

_MACOS_MALLOC_PREFIXES = ("Malloc",)
_CODEX_PATH_MARKERS = (
    "/codex-path",
    "/var/run/com.apple.security.cryptexd/codex.system/",
)


def sanitized_runtime_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment suitable for daemon/acpx child processes.

    The trading daemon is long-lived and should not inherit macOS allocator
    debugging toggles or Codex bootstrap path shims from the interactive shell.
    """
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith(_MACOS_MALLOC_PREFIXES):
            env.pop(key, None)

    path = env.get("PATH")
    if path:
        parts = [
            part
            for part in path.split(os.pathsep)
            if part and not any(marker in part for marker in _CODEX_PATH_MARKERS)
        ]
        env["PATH"] = os.pathsep.join(parts)
    return env
