"""Measured World cohort runtime identity. Operator YAML never supplies git_commit.

Two tiers: git_commit stays audit-only while lane_code_hash (content hash of
lane-protocol-adjacent ``trader/**/*.py`` minus the proven-disjoint denylist)
drives drift. Unrelated commits keep lanes learning; any lane-protocol code
change still fails closed. A test guard recomputes the lane import closure
and fails if the denylist ever intersects it — shrink the denylist then,
never widen the exception list silently.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from trader.domain.world_cohort import WorldRuntimeIdentity
from trader.support.metadata.code_version import current_code_version

LANE_IDENTITY_ROOTS = (
    "trader/application/world_model",
    "trader/runtime/world_model_runtime.py",
    "trader/runtime/world_about_runtime.py",
    "trader/runtime/world_macro_runtime.py",
    "trader/infrastructure/graph",
    "trader/infrastructure/state_db",
    "trader/support",
    "trader/domain/world_cohort.py",
    "trader/domain/world_context.py",
    "trader/domain/world_driver.py",
    "trader/domain/world_episode.py",
    "trader/domain/world_scope.py",
    "trader/domain/world_graph.py",
    "trader/domain/world_knowledge.py",
    "trader/domain/world_news.py",
    "trader/domain/world_company.py",
    "trader/domain/world_macro.py",
    "trader/domain/world_pattern.py",
    "trader/domain/world_feature_contract.py",
    "trader/domain/situation",
    "trader/domain/company",
    "trader/domain/semantic",
)

LANE_CODE_DENYLIST = (
    "trader/agent",
    "trader/application/analyst",
    "trader/application/cycle",
    "trader/application/decide",
    "trader/application/execute",
    "trader/application/exit",
    "trader/application/learnings",
    "trader/application/migration",
    "trader/application/portfolio",
    "trader/application/queue",
    "trader/application/record",
    "trader/application/universe",
    "trader/execution",
    "trader/infrastructure/market_sources",
    "trader/interfaces",
    "trader/market",
    "trader/planning",
    "trader/reporting",
)

LANE_CODE_HASHED = (
    # Lane-reachable files inside denied prefixes. rotation/ stays whole: its
    # lazy core edge is invisible to the AST guard, and it derives the mapping.
    # The __init__ files are empty, __all__-only, or pure re-exports from
    # already-hashed siblings (world_macro re-exports series).
    "trader/application/execute/__init__.py",
    "trader/application/execute/protocols.py",
    "trader/planning/__init__.py",
    "trader/planning/indicator_watch.py",
    "trader/market/__init__.py",
    "trader/market/features.py",
    "trader/market/radar.py",
    "trader/market/radar_config.py",
    "trader/market/radar_data.py",
    "trader/market/radar_shadow.py",
    "trader/market/rotation",
    "trader/infrastructure/market_sources/__init__.py",
    "trader/infrastructure/market_sources/commodity_prices.py",
    "trader/infrastructure/market_sources/macro_series.py",
    "trader/infrastructure/market_sources/world_macro/__init__.py",
    "trader/infrastructure/market_sources/world_macro/series.py",
    "trader/infrastructure/market_sources/world_scope_listing.py",
    "trader/reporting/__init__.py",
    "trader/reporting/read_models/__init__.py",
    "trader/reporting/read_models/world_evaluation.py",
    "trader/reporting/read_models/world_impact.py",
)


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _denied(relative_posix: str) -> bool:
    if not any(_under(relative_posix, prefix) for prefix in LANE_CODE_DENYLIST):
        return False
    return not any(_under(relative_posix, keeper) for keeper in LANE_CODE_HASHED)


def lane_code_hash(repo_root: str | Path) -> str:
    """Deterministic content hash of hashed ``trader/**/*.py``. Pure."""

    root = Path(repo_root)
    trader_dir = root / "trader"
    if not trader_dir.is_dir():
        raise ValueError(f"repo_root has no trader/ package: {root}")
    digest = hashlib.sha256()
    count = 0
    for path in sorted(trader_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if _denied(relative):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
        count += 1
    if not count:
        raise ValueError(f"no hashed python files under {trader_dir}")
    return digest.hexdigest()


class MeasuredWorldRuntimeIdentityService:
    """Attest lane code hash plus commit, build id, Python and NumPy versions."""

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
            lane_code_hash=lane_code_hash(self._repo_root),
        )


__all__ = [
    "LANE_CODE_DENYLIST",
    "LANE_CODE_HASHED",
    "LANE_IDENTITY_ROOTS",
    "MeasuredWorldRuntimeIdentityService",
    "lane_code_hash",
]
