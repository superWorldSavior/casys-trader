"""ACPX configuration and adapter identity capture."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path

from .common import _canonical_fingerprint, _clean, _stable_json_object
from .node_artifacts import _acpx_package_fingerprint

_AGENT_ALIASES = {"factory-droid": "droid", "factorydroid": "droid"}
_BUILT_IN_AGENT_ARGV: dict[str, tuple[str, ...]] = {
    "codex": ("npx", "-y", "@agentclientprotocol/codex-acp@^1.1.5"),
    "claude": ("npx", "-y", "@agentclientprotocol/claude-agent-acp@^0.60.0"),
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
    return _canonical_fingerprint(
        {
            "schema": "acpx_transport_v1",
            "resolved_entrypoint": str(resolved),
            "artifact_fingerprint": artifact_fingerprint,
        }
    )


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
    native_exec_forbidden = getattr(backend, "allow_native_exec", None) is False
    if exec_flag not in _AGENT_EXEC_TRUE or native_exec_forbidden:
        try:
            return Path.cwd().resolve(strict=True)
        except OSError:
            return None

    configured_home = _clean(getattr(backend, "codex_home", None)) or _clean(os.getenv("CODEX_HOME"))
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
    global_config = _stable_json_object(Path(os.path.expanduser("~")) / ".acpx" / "config.json")
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
        else global_default
        if isinstance(global_default, str)
        else "codex"
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


def _node_package_bin_entrypoint(package_root: Path, *, expected_name: str, expected_version: str) -> Path | None:
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
    configured_cache = _clean(os.getenv("npm_config_cache")) or _clean(os.getenv("NPM_CONFIG_CACHE"))
    cache = Path(configured_cache).expanduser() if configured_cache else Path(os.path.expanduser("~")) / ".npm"
    npx_root = cache / "_npx"
    relative_package = Path(*name.split("/"))
    try:
        cache_dirs = sorted(npx_root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None
    matches: list[tuple[Path, str]] = []
    for cache_dir in cache_dirs:
        package_root = cache_dir / "node_modules" / relative_package
        entrypoint = _node_package_bin_entrypoint(package_root, expected_name=name, expected_version=version)
        if entrypoint is None:
            continue
        fingerprint = _acpx_package_fingerprint(entrypoint)
        if fingerprint is None:
            return None
        matches.append((entrypoint, fingerprint))
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
            return None
        package_identity = _cached_npx_package_identity(*package)
        if package_identity is None:
            return None
    return _canonical_fingerprint(
        {
            "schema": "acpx_agent_adapter_v1",
            "agent": agent,
            "cwd": str(cwd),
            "argv": [str(resolved_executable), *argv[1:]],
            "resolved_executable": str(resolved_executable),
            "executable_fingerprint": executable_fingerprint,
            "package_identity": package_identity,
        }
    )


def _acpx_profile_fingerprint(*, agent_profile: str | None, transport: str | None, adapter: str | None) -> str | None:
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
        configured = _clean(getattr(backend, "codex_home", None)) or _clean(os.getenv("CODEX_HOME"))
        return (Path(configured).expanduser() if configured else repo_root / "ops" / "codex-home") / "config.toml"
    if agent == "kimi":
        configured = _clean(os.getenv("KIMI_CODE_HOME"))
        return (Path(configured).expanduser() if configured else repo_root / "ops" / "kimi-home") / "config.toml"
    if agent in {"grok", "grok-build"}:
        configured = _clean(getattr(backend, "grok_home", None)) or _clean(os.getenv("GROK_HOME"))
        return (Path(configured).expanduser() if configured else repo_root / "ops" / "grok-home") / "config.toml"
    return None


def _profile_config_identity(backend: object, *, repo_root: Path, agent: str) -> tuple[str | None, str | None]:
    """Return only non-secret profile evidence: effective effort and a digest."""

    config_path = _profile_config_path(backend, repo_root=repo_root, agent=agent)
    if config_path is None:
        return "provider-default", "provider-managed"
    try:
        payload = config_path.read_bytes()
        config = tomllib.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, None
    if not isinstance(config, Mapping):
        return None, None
    if agent == "kimi":
        thinking = config.get("thinking")
        thinking = thinking if isinstance(thinking, Mapping) else {}
        profile_effort = _clean(thinking.get("effort"))
        causal_profile = {
            "default_model": config.get("default_model"),
            "thinking": {"enabled": thinking.get("enabled"), "effort": thinking.get("effort")},
        }
    elif agent in {"grok", "grok-build"}:
        models = config.get("models")
        models = models if isinstance(models, Mapping) else {}
        profile_effort = _clean(models.get("default_reasoning_effort"))
        causal_profile = {
            "models": {
                "default": models.get("default"),
                "default_reasoning_effort": models.get("default_reasoning_effort"),
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
        causal_profile, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return profile_effort, "sha256:" + hashlib.sha256(canonical).hexdigest()
