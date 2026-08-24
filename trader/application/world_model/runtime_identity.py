"""Measured World cohort runtime identity. Operator YAML never supplies git_commit."""

from __future__ import annotations

import sys
from pathlib import Path

from trader.domain.world_cohort import WorldRuntimeIdentity
from trader.support.metadata.code_version import current_code_version


class MeasuredWorldRuntimeIdentityService:
    """Attest the running git commit, build id, Python and NumPy versions."""

    def __init__(self, *, repo_root: str | Path, application_build_id: str) -> None:
        self._repo_root = Path(repo_root)
        self._application_build_id = application_build_id

    def measure(self) -> WorldRuntimeIdentity:
        import numpy

        code = current_code_version(self._repo_root)
        commit = code.get("git_commit")
        if not isinstance(commit, str) or not commit.strip():
            raise ValueError("measured git_commit is unavailable")
        python_version = ".".join(str(part) for part in sys.version_info[:3])
        return WorldRuntimeIdentity(
            git_commit=commit.strip().lower(),
            python_version=python_version,
            numpy_version=str(numpy.__version__),
            application_build_id=self._application_build_id,
        )


__all__ = ["MeasuredWorldRuntimeIdentityService"]
