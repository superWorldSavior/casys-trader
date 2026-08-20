"""Node and ACPX executable fingerprinting primitives."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .common import _canonical_fingerprint, _clean, _stable_file_sha256, _stable_json_object

_RUNTIME_ARTIFACT_SUFFIXES = frozenset((".cjs", ".js", ".json", ".mjs", ".node", ".wasm"))
_NODE_LOCKFILE_NAMES = (
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
)


def _node_package_for_entrypoint(
    entrypoint: Path,
) -> tuple[Path, dict[str, object], str] | None:
    """Find the nearest package whose ``bin`` resolves to this entrypoint."""

    for package_root in (entrypoint.parent, *entrypoint.parents):
        manifest_path = package_root / "package.json"
        if not manifest_path.is_file():
            continue
        manifest_sha256 = _stable_file_sha256(manifest_path)
        if manifest_sha256 is None:
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        raw_bin = manifest.get("bin")
        if isinstance(raw_bin, str):
            targets = [raw_bin]
        elif isinstance(raw_bin, Mapping):
            targets = [value for value in raw_bin.values() if isinstance(value, str)]
        else:
            targets = []
        for target in targets:
            try:
                resolved_target = (package_root / target).resolve(strict=True)
            except OSError:
                return None
            if resolved_target == entrypoint:
                return package_root, manifest, manifest_sha256
    return None


def _is_runtime_artifact(path: Path) -> bool | None:
    name = path.name.lower()
    if name.endswith((".d.ts", ".map")):
        return False
    if path.suffix.lower() in _RUNTIME_ARTIFACT_SUFFIXES:
        return True
    try:
        return bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    except OSError:
        return None


def _runtime_tree_fingerprint(root: Path) -> str | None:
    """Hash executable/runtime files under a package subtree, not docs or maps."""

    def paths() -> tuple[Path, ...] | None:
        selected: list[Path] = []
        walk_errors: list[OSError] = []
        try:
            for directory, dirnames, filenames in os.walk(
                root,
                followlinks=False,
                onerror=walk_errors.append,
            ):
                dirnames[:] = sorted(
                    name for name in dirnames if name != "node_modules" and not (Path(directory) / name).is_symlink()
                )
                for filename in sorted(filenames):
                    candidate = Path(directory) / filename
                    selected_artifact = _is_runtime_artifact(candidate)
                    if selected_artifact is None:
                        return None
                    if selected_artifact:
                        selected.append(candidate)
        except OSError:
            return None
        if walk_errors:
            return None
        return tuple(sorted(selected, key=lambda path: path.relative_to(root).as_posix()))

    before = paths()
    if before is None or not before:
        return None
    rows: list[dict[str, str]] = []
    for path in before:
        digest = _stable_file_sha256(path)
        if digest is None:
            return None
        rows.append({"path": path.relative_to(root).as_posix(), "sha256": digest})
    if paths() != before:
        return None
    return _canonical_fingerprint(rows)


def _resolve_node_dependency(package_root: Path, name: str) -> Path | None:
    relative = Path(*name.split("/"))
    for ancestor in (package_root, *package_root.parents):
        candidate = ancestor / "node_modules" / relative
        try:
            if candidate.is_dir():
                return candidate.resolve(strict=True)
        except OSError:
            return None
    return None


def _nearest_node_lockfiles(package_root: Path) -> list[dict[str, str]] | None:
    for depth, ancestor in enumerate((package_root, *package_root.parents)):
        if depth > 4:
            break
        existing = [ancestor / name for name in _NODE_LOCKFILE_NAMES if (ancestor / name).is_file()]
        if not existing:
            continue
        rows: list[dict[str, str]] = []
        for path in existing:
            digest = _stable_file_sha256(path)
            if digest is None:
                return None
            rows.append({"name": path.name, "sha256": digest})
        return rows
    return []


def _node_package_graph_fingerprint(package_root: Path) -> str | None:
    """Hash the installed runtime dependency graph, including transitive code."""

    rows: list[dict[str, object]] = []
    visited: set[Path] = set()

    def visit(root: Path) -> bool:
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            return False
        if resolved_root in visited:
            return True
        visited.add(resolved_root)
        manifest_path = resolved_root / "package.json"
        manifest_sha256 = _stable_file_sha256(manifest_path)
        manifest = _stable_json_object(manifest_path)
        runtime_fingerprint = _runtime_tree_fingerprint(resolved_root)
        if manifest_sha256 is None or manifest is None or runtime_fingerprint is None:
            return False

        dependency_kinds: dict[str, set[str]] = {}
        for field in ("dependencies", "optionalDependencies", "peerDependencies"):
            raw_dependencies = manifest.get(field, {})
            if not isinstance(raw_dependencies, Mapping):
                return False
            for raw_name in raw_dependencies:
                name = _clean(raw_name)
                if name is None:
                    return False
                dependency_kinds.setdefault(name, set()).add(field)

        resolved_dependencies: list[dict[str, object]] = []
        for name in sorted(dependency_kinds):
            dependency_root = _resolve_node_dependency(resolved_root, name)
            kinds = sorted(dependency_kinds[name])
            if dependency_root is None:
                if kinds == ["dependencies"]:
                    return False
                resolved_dependencies.append({"name": name, "kinds": kinds, "state": "absent"})
                continue
            resolved_dependencies.append({"name": name, "kinds": kinds, "resolved_root": str(dependency_root)})
            if not visit(dependency_root):
                return False

        if _stable_file_sha256(manifest_path) != manifest_sha256:
            return False
        rows.append(
            {
                "resolved_root": str(resolved_root),
                "manifest": manifest_sha256,
                "runtime_tree": runtime_fingerprint,
                "dependencies": resolved_dependencies,
            }
        )
        return True

    if not visit(package_root):
        return None
    rows.sort(key=lambda row: str(row["resolved_root"]))
    return _canonical_fingerprint(rows)


def _acpx_package_fingerprint(entrypoint: Path) -> str | None:
    package = _node_package_for_entrypoint(entrypoint)
    if package is None:
        return _stable_file_sha256(entrypoint)
    package_root, _manifest, manifest_sha256 = package
    graph_fingerprint = _node_package_graph_fingerprint(package_root)
    if graph_fingerprint is None:
        return None
    lockfiles = _nearest_node_lockfiles(package_root)
    if lockfiles is None:
        return None
    if _stable_file_sha256(package_root / "package.json") != manifest_sha256:
        return None
    return _canonical_fingerprint(
        {
            "schema": "acpx_node_artifact_v1",
            "package_manifest": manifest_sha256,
            "runtime_graph": graph_fingerprint,
            "lockfiles": lockfiles,
        }
    )
