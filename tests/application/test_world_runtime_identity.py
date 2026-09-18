"""Two-tier runtime identity: git_commit is audit-only, lane_code_hash drifts."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from trader.application.world_model.runtime_identity import (
    LANE_CODE_DENYLIST,
    LANE_CODE_HASHED,
    LANE_IDENTITY_ROOTS,
    lane_code_hash,
)
from trader.domain.world_cohort import WorldRuntimeIdentity

HEX64 = re.compile(r"[0-9a-f]{64}")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _identity(**overrides: object) -> WorldRuntimeIdentity:
    values: dict[str, object] = {
        "git_commit": "b" * 40,
        "python_version": "3.11.9",
        "numpy_version": "1.26.4",
        "application_build_id": "casys-trader.world.shadow_pilot.v1",
        "lane_code_hash": "d" * 64,
    }
    values.update(overrides)
    return WorldRuntimeIdentity(**values)  # type: ignore[arg-type]


def test_commit_is_audit_only_lane_hash_drives_equality() -> None:
    base = _identity()
    assert base == _identity(git_commit="c" * 40)
    assert base != _identity(lane_code_hash="e" * 64)
    assert base != _identity(python_version="3.12.0")


def test_legacy_identity_without_lane_hash_never_matches() -> None:
    legacy = _identity(lane_code_hash=None)
    assert legacy != _identity()
    assert legacy == _identity(lane_code_hash=None, git_commit="c" * 40)


def test_from_mapping_accepts_legacy_payload_without_lane_hash() -> None:
    legacy = WorldRuntimeIdentity.from_mapping(
        {
            "git_commit": "b" * 40,
            "python_version": "3.11.9",
            "numpy_version": "1.26.4",
            "application_build_id": "casys-trader.world.shadow_pilot.v1",
        }
    )
    assert legacy.lane_code_hash is None
    assert legacy != _identity()
    assert "lane_code_hash" not in legacy.to_dict()
    assert WorldRuntimeIdentity.from_mapping(_identity().to_dict()) == _identity()


def test_lane_code_hash_is_deterministic_hex_on_live_tree() -> None:
    first = lane_code_hash(REPO_ROOT)
    second = lane_code_hash(REPO_ROOT)
    assert first == second
    assert HEX64.fullmatch(first)


def test_lane_code_hash_ignores_denylisted_non_python_and_caches(tmp_path: Path) -> None:
    hashed = tmp_path / "trader" / "application" / "world_model"
    hashed.mkdir(parents=True)
    (hashed / "a.py").write_text("X = 1\n", encoding="utf-8")
    denied = tmp_path / "trader" / "reporting"
    denied.mkdir(parents=True)
    (denied / "r.py").write_text("Y = 1\n", encoding="utf-8")
    before = lane_code_hash(tmp_path)
    (denied / "r.py").write_text("Y = 2  # denylisted edits never drift\n", encoding="utf-8")
    (denied / "notes.txt").write_text("data\n", encoding="utf-8")
    cache = denied / "__pycache__"
    cache.mkdir()
    (cache / "r.pyc").write_bytes(b"\x00")
    assert lane_code_hash(tmp_path) == before
    (hashed / "a.py").write_text("X = 2  # protocol edits drift\n", encoding="utf-8")
    assert lane_code_hash(tmp_path) != before


def _module_of(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _first_party_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name == "trader" or a.name.startswith("trader."))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "trader" or node.module.startswith("trader."):
                found.add(node.module)
    return found


def test_guard_denylist_disjoint_from_lane_import_closure() -> None:
    """Any lane-reachable denylisted file fails here: shrink the denylist, never widen."""

    graph: dict[str, set[str]] = {}
    for py in (REPO_ROOT / "trader").rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        graph[_module_of(py)] = _first_party_imports(py)

    def expand(root: str) -> set[str]:
        target = REPO_ROOT / root
        if target.is_file():
            return {_module_of(target)}
        return {_module_of(f) for f in target.rglob("*.py") if "__pycache__" not in f.parts}

    seeds = {module for root in LANE_IDENTITY_ROOTS for module in expand(root)}
    reachable, frontier = set(seeds), list(seeds)
    while frontier:
        for dep in graph.get(frontier.pop(), ()):
            while dep not in graph and "." in dep:
                dep = dep.rpartition(".")[0]
            if dep in graph and dep not in reachable:
                reachable.add(dep)
                frontier.append(dep)
    # Importing a submodule executes its ancestor packages: they are reached too.
    for module in list(reachable):
        package = module.rpartition(".")[0]
        while package:
            if package in graph:
                reachable.add(package)
            package = package.rpartition(".")[0]

    denied: set[str] = set()
    for prefix in LANE_CODE_DENYLIST:
        denied.update(expand(prefix))
    for keeper in LANE_CODE_HASHED:
        denied.difference_update(expand(keeper))
    assert denied.isdisjoint(reachable)
