"""Stable, non-secret experiment identity for decision outcome cohorts."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import shlex
import shutil
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 3
ID_PREFIX = "exp:v3:"
V2_ID_PREFIX = "exp:v2:"
LEGACY_ID_PREFIX = "exp:v1:"

_PRESET_MARKER = re.compile(
    r"^# >>> casys:model-preset=(?P<name>[\w.-]+) >>>$"
)
_NUMERIC_RISK_FIELDS = (
    "max_position_value",
    "max_gross_exposure",
    "max_order_value",
    "min_equity",
    "max_risk_per_trade_pct",
    "min_trade_confidence",
    "full_risk_confidence",
)
_BOOLEAN_RISK_FIELDS = (
    "confidence_gate_enabled",
    "require_hard_stop",
)
_RISK_FIELDS = frozenset((*_NUMERIC_RISK_FIELDS, *_BOOLEAN_RISK_FIELDS))
_POSITIVE_RISK_FIELDS = frozenset(
    (
        "max_position_value",
        "max_gross_exposure",
        "max_order_value",
        "max_risk_per_trade_pct",
    )
)
_CONFIDENCE_RISK_FIELDS = frozenset(
    ("min_trade_confidence", "full_risk_confidence")
)
_MODEL_PROFILE_FIELDS = frozenset(
    (
        "configured_model",
        "transport",
        "agent",
        "reasoning_effort",
        "profile_fingerprint",
    )
)
_RUNTIME_ARTIFACT_SUFFIXES = frozenset(
    (".cjs", ".js", ".json", ".mjs", ".node", ".wasm")
)
_NODE_LOCKFILE_NAMES = (
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
)
_AGENT_ALIASES = {
    "factory-droid": "droid",
    "factorydroid": "droid",
}
_BUILT_IN_AGENT_ARGV: dict[str, tuple[str, ...]] = {
    "codex": (
        "npx",
        "-y",
        "@agentclientprotocol/codex-acp@^1.1.5",
    ),
    "claude": (
        "npx",
        "-y",
        "@agentclientprotocol/claude-agent-acp@^0.60.0",
    ),
    "grok-build": ("grok", "agent", "stdio"),
    "kimi": ("kimi", "acp"),
}
_EXACT_SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_AGENT_EXEC_TRUE = frozenset(("1", "true", "yes", "on"))


def commission_model_identity(value: object | None) -> str:
    """Return a stable, non-sensitive name for the active fee policy."""

    if value is None:
        return "none"
    class_name = type(value).__name__
    known = {
        "IbkrCommissionModel": "ibkr",
        "NoCommissionModel": "none",
    }
    if class_name in known:
        return known[class_name]
    return f"custom:{type(value).__module__}.{type(value).__qualname__}"


def active_model_preset(env_path: str | Path) -> str | None:
    """Read only the generated preset marker, never dotenv assignments."""

    try:
        lines = Path(env_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        match = _PRESET_MARKER.fullmatch(line.strip())
        if match is not None:
            return match.group("name")
    return None


def _clean(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _canonical_fingerprint(value: object) -> str | None:
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _stable_file_sha256(path: Path) -> str | None:
    """Hash one regular file while rejecting concurrent replacement/mutation."""

    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            digest = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        current = path.stat()
    except OSError:
        return None
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(
        getattr(before, field) != getattr(after, field)
        or getattr(after, field) != getattr(current, field)
        for field in stable_fields
    ):
        return None
    return "sha256:" + digest.hexdigest()


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
                    name
                    for name in dirnames
                    if name != "node_modules"
                    and not (Path(directory) / name).is_symlink()
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
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": digest,
            }
        )
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
        existing = [
            ancestor / name
            for name in _NODE_LOCKFILE_NAMES
            if (ancestor / name).is_file()
        ]
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
        if (
            manifest_sha256 is None
            or manifest is None
            or runtime_fingerprint is None
        ):
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
                # Optional/peer dependencies may legitimately be absent, but
                # their absence is still part of the installed graph identity.
                if kinds == ["dependencies"]:
                    return False
                resolved_dependencies.append(
                    {"name": name, "kinds": kinds, "state": "absent"}
                )
                continue
            resolved_dependencies.append(
                {
                    "name": name,
                    "kinds": kinds,
                    "resolved_root": str(dependency_root),
                }
            )
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
    # Reject a package manifest changed after it selected the entrypoint and
    # dependency graph above.
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


def _acpx_transport_fingerprint(backend: object) -> str | None:
    """Identify the effective ACPX entrypoint without persisting its path."""

    configured = _clean(getattr(backend, "acpx_bin", None))
    if configured is None:
        return None
    resolved_raw = shutil.which(configured)
    if resolved_raw is None:
        return None
    try:
        resolved = Path(resolved_raw).resolve(strict=True)
    except OSError:
        return None
    artifact_fingerprint = _acpx_package_fingerprint(resolved)
    if artifact_fingerprint is None:
        return None
    # The resolved path is causal (two independent wrappers may have identical
    # bytes today and diverge later), but it is kept inside the digest so local
    # usernames and deployment layout never enter decision ledgers.
    return _canonical_fingerprint(
        {
            "schema": "acpx_transport_v1",
            "resolved_entrypoint": str(resolved),
            "artifact_fingerprint": artifact_fingerprint,
        }
    )


def _stable_json_object(path: Path) -> dict[str, object] | None:
    """Read one JSON object atomically enough for cohort identity capture."""

    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            payload = handle.read()
            after = os.fstat(handle.fileno())
        current = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(
        getattr(before, field) != getattr(after, field)
        or getattr(after, field) != getattr(current, field)
        for field in stable_fields
    ):
        return None
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _configured_agent_argv(value: object) -> tuple[str, ...] | None:
    """Parse the ACPX agent config forms without shell expansion."""

    if not isinstance(value, Mapping):
        return None
    if "argv" in value:
        if "command" in value or "args" in value:
            return None
        raw_argv = value.get("argv")
        if (
            not isinstance(raw_argv, list)
            or not raw_argv
            or not all(isinstance(item, str) for item in raw_argv)
            or not raw_argv[0]
        ):
            return None
        return tuple(raw_argv)

    command = value.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    command = command.strip()
    if "args" in value:
        raw_args = value.get("args")
        if (
            re.search(r"[\s'\"]", command)
            or not isinstance(raw_args, list)
            or not all(isinstance(item, str) for item in raw_args)
        ):
            return None
        return (command, *raw_args)
    try:
        argv = tuple(shlex.split(command, posix=True))
    except ValueError:
        return None
    return argv or None


def _acpx_initial_cwd(backend: object) -> Path | None:
    """Mirror the cwd ACPX uses to select its project config."""

    exec_flag = (_clean(os.getenv("CASYS_AGENT_EXEC")) or "").lower()
    if exec_flag not in _AGENT_EXEC_TRUE:
        try:
            return Path.cwd().resolve(strict=True)
        except OSError:
            return None

    configured_home = _clean(getattr(backend, "codex_home", None)) or _clean(
        os.getenv("CODEX_HOME")
    )
    if configured_home is None or not os.path.isabs(configured_home):
        return None
    home = Path(os.path.realpath(configured_home))
    override = _clean(os.getenv("CASYS_AGENT_EXEC_CWD"))
    candidate = Path(os.path.realpath(override)) if override else home / "calc-scratch"
    try:
        candidate.relative_to(home)
    except ValueError:
        return None
    return candidate


def _acpx_configured_agents(
    backend: object,
) -> tuple[str, dict[str, tuple[str, ...]]] | None:
    """Resolve ACPX global/project agent config, excluding auth and secrets."""

    cwd = _acpx_initial_cwd(backend)
    if cwd is None:
        return None
    global_config = _stable_json_object(
        Path(os.path.expanduser("~")) / ".acpx" / "config.json"
    )
    project_config = _stable_json_object(cwd / ".acpxrc.json")
    if global_config is None or project_config is None:
        return None

    def parsed_default(config: Mapping[str, object]) -> str | None | bool:
        raw = config.get("defaultAgent")
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            return False
        return raw.strip().lower()

    global_default = parsed_default(global_config)
    project_default = parsed_default(project_config)
    if global_default is False or project_default is False:
        return None

    merged: dict[str, tuple[str, ...]] = {}
    for config in (global_config, project_config):
        raw_agents = config.get("agents")
        if raw_agents is None:
            continue
        if not isinstance(raw_agents, Mapping):
            return None
        for raw_name, raw_value in raw_agents.items():
            name = _clean(raw_name)
            argv = _configured_agent_argv(raw_value)
            if name is None or argv is None:
                return None
            merged[name.lower()] = argv

    default_agent = (
        project_default
        if isinstance(project_default, str)
        else global_default if isinstance(global_default, str) else "codex"
    )
    return default_agent, merged


def _effective_agent_invocation(
    backend: object,
) -> tuple[str, tuple[str, ...], Path] | None:
    configured = _acpx_configured_agents(backend)
    cwd = _acpx_initial_cwd(backend)
    if configured is None or cwd is None:
        return None
    default_agent, agents = configured
    explicit = _clean(getattr(backend, "agent", None))
    agent = default_agent if explicit is None or explicit.lower() == "default" else explicit.lower()
    canonical_agent = _AGENT_ALIASES.get(agent, agent)
    argv = agents.get(agent) or agents.get(canonical_agent)
    if argv is None:
        argv = _BUILT_IN_AGENT_ARGV.get(canonical_agent, (agent,))
    return canonical_agent, argv, cwd


def _exact_npm_package_spec(value: str) -> tuple[str, str] | None:
    if value.startswith("@"):
        slash = value.find("/")
        separator = value.rfind("@")
        if slash < 2 or separator <= slash:
            return None
    else:
        separator = value.rfind("@")
        if separator <= 0:
            return None
    name, version = value[:separator], value[separator + 1 :]
    if not name or _EXACT_SEMVER.fullmatch(version) is None:
        return None
    return name, version


def _node_package_bin_entrypoint(
    package_root: Path,
    *,
    expected_name: str,
    expected_version: str,
) -> Path | None:
    manifest = _stable_json_object(package_root / "package.json")
    if manifest is None:
        return None
    if manifest.get("name") != expected_name or manifest.get("version") != expected_version:
        return None
    raw_bin = manifest.get("bin")
    if isinstance(raw_bin, str):
        relative = raw_bin
    elif isinstance(raw_bin, Mapping):
        preferred = expected_name.rsplit("/", 1)[-1]
        candidates = [value for value in raw_bin.values() if isinstance(value, str)]
        relative = raw_bin.get(preferred)
        if not isinstance(relative, str):
            relative = candidates[0] if len(candidates) == 1 else None
    else:
        relative = None
    if not isinstance(relative, str):
        return None
    try:
        return (package_root / relative).resolve(strict=True)
    except OSError:
        return None


def _cached_npx_package_identity(name: str, version: str) -> str | None:
    configured_cache = _clean(os.getenv("npm_config_cache")) or _clean(
        os.getenv("NPM_CONFIG_CACHE")
    )
    cache = (
        Path(configured_cache).expanduser()
        if configured_cache
        else Path(os.path.expanduser("~")) / ".npm"
    )
    npx_root = cache / "_npx"
    relative_package = Path(*name.split("/"))
    try:
        cache_dirs = sorted(npx_root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None
    matches: list[tuple[Path, str]] = []
    for cache_dir in cache_dirs:
        package_root = cache_dir / "node_modules" / relative_package
        entrypoint = _node_package_bin_entrypoint(
            package_root,
            expected_name=name,
            expected_version=version,
        )
        if entrypoint is None:
            continue
        fingerprint = _acpx_package_fingerprint(entrypoint)
        if fingerprint is None:
            return None
        matches.append((entrypoint, fingerprint))
    # npm chooses an opaque cache directory. Without exactly one matching
    # resolution we cannot prove which artifact a new exec will launch.
    if len(matches) != 1:
        return None
    entrypoint, fingerprint = matches[0]
    return _canonical_fingerprint(
        {
            "schema": "npx_cached_package_v1",
            "resolved_entrypoint": str(entrypoint),
            "artifact_fingerprint": fingerprint,
        }
    )


def _resolved_executable_identity(executable: str) -> tuple[Path, str] | None:
    resolved_raw = shutil.which(executable)
    if resolved_raw is None:
        return None
    try:
        resolved = Path(resolved_raw).resolve(strict=True)
    except OSError:
        return None
    fingerprint = _acpx_package_fingerprint(resolved)
    if fingerprint is None:
        return None
    return resolved, fingerprint


def _agent_adapter_fingerprint(
    invocation: tuple[str, tuple[str, ...], Path],
) -> str | None:
    """Fingerprint the adapter ACPX will spawn, fail-closing floating execs."""

    agent, argv, cwd = invocation
    if not argv:
        return None
    executable = _resolved_executable_identity(argv[0])
    if executable is None:
        return None
    resolved_executable, executable_fingerprint = executable
    executable_name = resolved_executable.name.lower()

    package_identity: str | None = None
    if executable_name in {"npx", "npx-cli.js"} or Path(argv[0]).name.lower() == "npx":
        package_token: str | None = None
        for token in argv[1:]:
            if token in {"-y", "--yes"}:
                continue
            if token.startswith("-"):
                return None
            package_token = token
            break
        package = _exact_npm_package_spec(package_token or "")
        if package is None:
            # Tags and semver ranges may resolve to new adapter code between
            # calls, so they can never identify a decision-grade cohort.
            return None
        package_identity = _cached_npx_package_identity(*package)
        if package_identity is None:
            return None

    return _canonical_fingerprint(
        {
            "schema": "acpx_agent_adapter_v1",
            "agent": agent,
            # Paths/arguments are causal but may expose local layout or tokens;
            # they only ever enter this opaque digest.
            "cwd": str(cwd),
            "argv": [str(resolved_executable), *argv[1:]],
            "resolved_executable": str(resolved_executable),
            "executable_fingerprint": executable_fingerprint,
            "package_identity": package_identity,
        }
    )


def _acpx_profile_fingerprint(
    *,
    agent_profile: str | None,
    transport: str | None,
    adapter: str | None,
) -> str | None:
    if agent_profile is None or transport is None or adapter is None:
        return None
    return _canonical_fingerprint(
        {
            "schema": "acpx_execution_profile_v2",
            "agent_profile": agent_profile,
            "transport": transport,
            "adapter": adapter,
        }
    )


def _profile_config_path(backend: object, *, repo_root: Path, agent: str) -> Path | None:
    if agent in {"default", "codex"}:
        configured = _clean(getattr(backend, "codex_home", None)) or _clean(
            os.getenv("CODEX_HOME")
        )
        return (
            Path(configured).expanduser() if configured else repo_root / "ops" / "codex-home"
        ) / "config.toml"
    if agent == "kimi":
        configured = _clean(os.getenv("KIMI_CODE_HOME"))
        return (
            Path(configured).expanduser() if configured else repo_root / "ops" / "kimi-home"
        ) / "config.toml"
    if agent in {"grok", "grok-build"}:
        configured = _clean(getattr(backend, "grok_home", None)) or _clean(
            os.getenv("GROK_HOME")
        )
        return (
            Path(configured).expanduser() if configured else repo_root / "ops" / "grok-home"
        ) / "config.toml"
    return None


def _profile_config_identity(
    backend: object,
    *,
    repo_root: Path,
    agent: str,
) -> tuple[str | None, str | None]:
    """Return only non-secret profile evidence: effective effort and a digest."""

    config_path = _profile_config_path(backend, repo_root=repo_root, agent=agent)
    if config_path is None:
        return "provider-default", "provider-managed"
    try:
        payload = config_path.read_bytes()
        config = tomllib.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, None

    if agent == "kimi":
        thinking = config.get("thinking")
        thinking = thinking if isinstance(thinking, Mapping) else {}
        profile_effort = _clean(thinking.get("effort"))
        causal_profile = {
            "default_model": config.get("default_model"),
            "thinking": {
                "enabled": thinking.get("enabled"),
                "effort": thinking.get("effort"),
            },
        }
    elif agent in {"grok", "grok-build"}:
        models = config.get("models")
        models = models if isinstance(models, Mapping) else {}
        profile_effort = _clean(models.get("default_reasoning_effort"))
        causal_profile = {
            "models": {
                "default": models.get("default"),
                "default_reasoning_effort": models.get(
                    "default_reasoning_effort"
                ),
            }
        }
    else:
        profile_effort = _clean(config.get("model_reasoning_effort"))
        causal_profile = {
            key: config.get(key)
            for key in (
                "model",
                "model_reasoning_effort",
                "personality",
                "service_tier",
                "sandbox_mode",
                "approval_policy",
            )
        }
    canonical = json.dumps(
        causal_profile,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return profile_effort, "sha256:" + hashlib.sha256(canonical).hexdigest()


def _provider_endpoint_fingerprint(backend: object) -> str | None:
    """Hash a causal endpoint projection without credentials or URL secrets."""

    base_url = _clean(getattr(backend, "base_url", None))
    if base_url is None:
        return None
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    effective_port = port or (443 if scheme == "https" else 80)
    # OpenAICompatibleBackend strips trailing slashes before appending its API
    # route, so equivalent spellings must keep the same cohort fingerprint.
    path = parsed.path.rstrip("/") or "/"
    causal_endpoint = {
        "backend": type(backend).__name__,
        "scheme": scheme,
        "host": host,
        "port": effective_port,
        "path": path,
    }
    canonical = json.dumps(
        causal_endpoint,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def model_profiles_from_backends(
    backends: object,
    *,
    repo_root: str | Path,
) -> dict[str, dict[str, object]]:
    """Capture effective, non-secret model execution profiles at daemon boot."""

    profiles: dict[str, dict[str, object]] = {}
    acpx_transport_cache: dict[str, str | None] = {}
    candidates = backends if isinstance(backends, (list, tuple)) else ()
    for backend in candidates:
        provider = _clean(getattr(backend, "provider", None))
        model = _clean(getattr(backend, "model", None))
        if provider is None:
            continue
        is_acpx = type(backend).__name__ == "AcpxBackend"
        if is_acpx:
            invocation = _effective_agent_invocation(backend)
            configured_agent = _clean(getattr(backend, "agent", None)) or "default"
            agent = invocation[0] if invocation is not None else configured_agent.lower()
            profile_effort, agent_profile_fingerprint = _profile_config_identity(
                backend,
                repo_root=Path(repo_root),
                agent=agent,
            )
            configured_acpx_bin = _clean(getattr(backend, "acpx_bin", None)) or ""
            if configured_acpx_bin not in acpx_transport_cache:
                acpx_transport_cache[configured_acpx_bin] = _acpx_transport_fingerprint(
                    backend
                )
            profile_fingerprint = _acpx_profile_fingerprint(
                agent_profile=agent_profile_fingerprint,
                transport=acpx_transport_cache[configured_acpx_bin],
                adapter=(
                    _agent_adapter_fingerprint(invocation)
                    if invocation is not None
                    else None
                ),
            )
            explicit_effort = _clean(getattr(backend, "reasoning_effort", None))
            profile = {
                "configured_model": model,
                "transport": "acpx",
                "agent": agent,
                "reasoning_effort": explicit_effort or profile_effort,
                "profile_fingerprint": profile_fingerprint,
            }
        else:
            profile = {
                "configured_model": model,
                "transport": "openai-compatible",
                "agent": None,
                "reasoning_effort": None,
                "profile_fingerprint": _provider_endpoint_fingerprint(backend),
            }
        profiles[provider] = profile
    return profiles


def _validated_risk(
    risk_policy: object,
) -> tuple[dict[str, object], list[str]]:
    if not isinstance(risk_policy, Mapping):
        return {}, ["risk:missing_or_invalid"]

    keys = {str(key) for key in risk_policy}
    issues: list[str] = []
    missing = sorted(_RISK_FIELDS - keys)
    unexpected = sorted(keys - _RISK_FIELDS)
    if missing:
        issues.append("risk:missing_fields:" + ",".join(missing))
    if unexpected:
        issues.append("risk:unexpected_fields:" + ",".join(unexpected))

    risk: dict[str, object] = {}
    for field in _NUMERIC_RISK_FIELDS:
        if field not in risk_policy:
            continue
        raw = risk_policy[field]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            issues.append(f"risk.{field}:invalid_type")
            continue
        value = float(raw)
        if not math.isfinite(value):
            issues.append(f"risk.{field}:non_finite")
            continue
        if field in _POSITIVE_RISK_FIELDS and value <= 0.0:
            issues.append(f"risk.{field}:must_be_positive")
            continue
        if field == "min_equity" and value < 0.0:
            issues.append("risk.min_equity:must_be_non_negative")
            continue
        if field == "max_risk_per_trade_pct" and value > 1.0:
            issues.append("risk.max_risk_per_trade_pct:out_of_range")
            continue
        if field in _CONFIDENCE_RISK_FIELDS and not 0.0 <= value <= 1.0:
            issues.append(f"risk.{field}:out_of_range")
            continue
        risk[field] = value

    for field in _BOOLEAN_RISK_FIELDS:
        if field not in risk_policy:
            continue
        raw = risk_policy[field]
        if not isinstance(raw, bool):
            issues.append(f"risk.{field}:invalid_type")
            continue
        risk[field] = raw

    low = risk.get("min_trade_confidence")
    high = risk.get("full_risk_confidence")
    if isinstance(low, float) and isinstance(high, float) and low > high:
        issues.append("risk.confidence_thresholds:min_exceeds_full")
    return risk, issues


def _validated_model_profiles(
    value: object,
    *,
    report_profile_issues: bool = True,
    selected_provider: str | None = None,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    if not isinstance(value, Mapping):
        return {}, ["model.profiles:missing_or_invalid"]
    profiles: dict[str, dict[str, object]] = {}
    issues: list[str] = []

    def report(provider: str) -> bool:
        return report_profile_issues and (
            selected_provider is None or selected_provider == provider
        )

    for raw_provider, raw_profile in value.items():
        provider = _clean(raw_provider)
        if provider is None or not isinstance(raw_profile, Mapping):
            if report_profile_issues and selected_provider is None:
                issues.append("model.profile:invalid")
            continue
        keys = {str(key) for key in raw_profile}
        if keys != _MODEL_PROFILE_FIELDS:
            if report(provider):
                issues.append(f"model.profile.{provider}:invalid_fields")
            continue
        configured_model = _clean(raw_profile.get("configured_model"))
        transport = _clean(raw_profile.get("transport"))
        fingerprint = _clean(raw_profile.get("profile_fingerprint"))
        agent = raw_profile.get("agent")
        effort = raw_profile.get("reasoning_effort")
        if configured_model is None or transport is None or fingerprint is None:
            if report(provider):
                issues.append(f"model.profile.{provider}:incomplete")
        if agent is not None and _clean(agent) is None:
            if report(provider):
                issues.append(f"model.profile.{provider}.agent:invalid")
        if effort is not None and _clean(effort) is None:
            if report(provider):
                issues.append(f"model.profile.{provider}.reasoning_effort:invalid")
        if transport == "acpx" and (_clean(agent) is None or _clean(effort) is None):
            if report(provider):
                issues.append(f"model.profile.{provider}:acpx_identity_incomplete")
        profiles[provider] = {
            "configured_model": configured_model,
            "transport": transport,
            "agent": _clean(agent),
            "reasoning_effort": _clean(effort),
            "profile_fingerprint": fingerprint,
        }
    if not profiles:
        issues.append("model.profiles:empty")
    return profiles, issues


def build_experiment_context(
    *,
    code_version: Mapping[str, object],
    model_preset: str | None,
    risk_policy: Mapping[str, object],
    commission_model: str | None,
    model_profiles: Mapping[str, Mapping[str, object]],
    runtime_issues: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the cycle-stable inputs needed for per-decision identity."""

    risk, risk_issues = _validated_risk(risk_policy)
    profiles, profile_issues = _validated_model_profiles(
        model_profiles,
        report_profile_issues=False,
    )
    issues = [str(issue) for issue in runtime_issues]
    issues.extend(risk_issues)
    issues.extend(profile_issues)

    git_commit = str(code_version.get("git_commit") or "").strip() or None
    if git_commit is None:
        issues.append("git_commit:missing")
    worktree_dirty = code_version.get("git_dirty")
    tracked_dirty = code_version.get("git_tracked_dirty")
    if not isinstance(tracked_dirty, bool):
        # Compatibility with callers that predate the tracked/untracked split.
        tracked_dirty = worktree_dirty if isinstance(worktree_dirty, bool) else None
    if not isinstance(tracked_dirty, bool):
        issues.append("git_tracked_dirty:missing_or_invalid")
        tracked_dirty = None
    elif tracked_dirty:
        issues.append("git_tracked_dirty:working_tree_not_clean")
    preset = str(model_preset or "").strip() or None
    if preset is None:
        issues.append("model.preset:missing")
    commission = str(commission_model or "").strip() or None
    if commission is None:
        issues.append("execution.commission_model:missing")
    return {
        "schema_version": SCHEMA_VERSION,
        "git_commit": git_commit,
        "git_worktree_dirty": (
            worktree_dirty if isinstance(worktree_dirty, bool) else None
        ),
        "git_tracked_dirty": tracked_dirty,
        "model_preset": preset,
        "commission_model": commission,
        "risk": risk,
        "model_profiles": profiles,
        "issues": list(dict.fromkeys(issues)),
    }


def decision_experiment(
    context: Mapping[str, object] | None,
    *,
    provider: object,
    model: object,
) -> dict[str, Any]:
    """Resolve one decision's versioned cohort descriptor and stable hash."""

    source = context if isinstance(context, Mapping) else {}
    issues = [str(value) for value in source.get("issues", [])] if isinstance(source.get("issues"), list) else []
    provider_name = str(provider or "").strip() or None
    model_name = str(model or "").strip() or None
    if provider_name is None:
        issues.append("model.provider:missing")
    if model_name is None:
        issues.append("model.model:missing")

    risk_values, risk_issues = _validated_risk(source.get("risk"))
    issues.extend(risk_issues)
    profiles, profile_issues = _validated_model_profiles(
        source.get("model_profiles"),
        selected_provider=provider_name,
    )
    issues.extend(profile_issues)
    profile = profiles.get(provider_name or "")
    if profile is None:
        issues.append(f"model.profile.{provider_name or 'unknown'}:missing")
        execution_profile: dict[str, object] = {}
    elif profile.get("configured_model") != model_name:
        issues.append("model.profile:configured_model_mismatch")
        execution_profile = {
            key: profile[key]
            for key in _MODEL_PROFILE_FIELDS
            if key != "configured_model"
        }
    else:
        execution_profile = {
            key: profile[key]
            for key in _MODEL_PROFILE_FIELDS
            if key != "configured_model"
        }
    components = {
        "git_commit": source.get("git_commit"),
        "git_tracked_dirty": source.get("git_tracked_dirty"),
        "model": {
            "provider": provider_name,
            "model": model_name,
            "preset": source.get("model_preset"),
            "execution_profile": execution_profile,
        },
        "execution": {
            "commission_model": source.get("commission_model"),
        },
        "risk": risk_values,
    }
    unique_issues = list(dict.fromkeys(issues))
    experiment_id = None
    if not unique_issues:
        experiment_id = _experiment_id(components)
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "components": components,
        "issues": unique_issues,
        "decision_grade": (
            experiment_id is not None
            and source.get("git_tracked_dirty") is False
        ),
    }


def _experiment_id(
    components: Mapping[str, object], *, prefix: str = ID_PREFIX
) -> str:
    canonical = json.dumps(
        components,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return prefix + hashlib.sha256(canonical).hexdigest()


def _id_schema(experiment_id: str) -> tuple[int, str] | None:
    if experiment_id.startswith(ID_PREFIX):
        return SCHEMA_VERSION, ID_PREFIX
    if experiment_id.startswith(V2_ID_PREFIX):
        return 2, V2_ID_PREFIX
    if experiment_id.startswith(LEGACY_ID_PREFIX):
        return 1, LEGACY_ID_PREFIX
    return None


def _components_are_valid(components: Mapping[str, object], *, schema_version: int) -> bool:
    risk, risk_issues = _validated_risk(components.get("risk"))
    if risk_issues or set(risk) != _RISK_FIELDS:
        return False
    if _clean(components.get("git_commit")) is None:
        return False
    if components.get("git_tracked_dirty") is not False:
        return False
    model = components.get("model")
    execution = components.get("execution")
    if not isinstance(model, Mapping) or not isinstance(execution, Mapping):
        return False
    expected_model_fields = {"provider", "model", "preset"}
    if schema_version >= 2:
        expected_model_fields.add("execution_profile")
    if {str(key) for key in model} != expected_model_fields:
        return False
    if not all(_clean(model.get(key)) is not None for key in ("provider", "model", "preset")):
        return False
    if {str(key) for key in execution} != {"commission_model"}:
        return False
    if _clean(execution.get("commission_model")) is None:
        return False
    if schema_version >= 2:
        profile = model.get("execution_profile")
        if not isinstance(profile, Mapping):
            return False
        if {str(key) for key in profile} != _MODEL_PROFILE_FIELDS - {"configured_model"}:
            return False
        if _clean(profile.get("transport")) is None:
            return False
        if _clean(profile.get("profile_fingerprint")) is None:
            return False
        if profile.get("transport") == "acpx" and (
            _clean(profile.get("agent")) is None
            or _clean(profile.get("reasoning_effort")) is None
        ):
            return False
    return True


def inherited_experiment(value: object) -> dict[str, Any] | None:
    """Accept a complete experiment descriptor propagated by an armed plan."""

    if not isinstance(value, Mapping):
        return None
    experiment_id = value.get("experiment_id")
    components = value.get("components")
    if not isinstance(experiment_id, str):
        return None
    version_and_prefix = _id_schema(experiment_id)
    if version_and_prefix is None:
        return None
    schema_version, prefix = version_and_prefix
    if not isinstance(components, Mapping):
        return None
    try:
        expected_id = _experiment_id(components, prefix=prefix)
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(experiment_id, expected_id):
        return None
    stored_schema = value.get("schema_version")
    if stored_schema is not None and stored_schema != schema_version:
        return None
    if not _components_are_valid(components, schema_version=schema_version):
        return None
    return {
        "schema_version": schema_version,
        "experiment_id": experiment_id,
        "components": dict(components),
        "issues": [],
        "decision_grade": value.get("decision_grade") is True,
    }


__all__ = [
    "ID_PREFIX",
    "LEGACY_ID_PREFIX",
    "SCHEMA_VERSION",
    "V2_ID_PREFIX",
    "active_model_preset",
    "build_experiment_context",
    "commission_model_identity",
    "decision_experiment",
    "inherited_experiment",
    "model_profiles_from_backends",
]
