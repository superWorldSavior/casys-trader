"""Validated, secret-free governance artifact fingerprints for process runs."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
PROCESS_CONFIG_PATH = "config/process_governance.yaml"
REQUIRED_ARTIFACT_PATHS = (
    PROCESS_CONFIG_PATH,
    "mandate/mandate.md",
    "mandate/memory.md",
    "config/risk.yaml",
    "trader/agent/protocol/prompts.py",
    "trader/agent/protocol/llm_schema.py",
)
OPTIONAL_ARTIFACT_PATHS = ("mandate/guardrails.json",)
TERMINAL_OUTCOMES = frozenset({"completed", "failed", "escalated", "cancelled"})
REQUIRED_NON_TERMINAL_STATES = frozenset({"recovery_required"})
RUNTIME_BINDING_STATUSES = frozenset({"not_integrated", "integrated"})

__all__ = [
    "OPTIONAL_ARTIFACT_PATHS",
    "PROCESS_CONFIG_PATH",
    "REQUIRED_ARTIFACT_PATHS",
    "GovernanceConfigError",
    "current_governance_version",
    "load_process_governance",
]


class GovernanceConfigError(ValueError):
    """Raised when the process governance contract is missing or malformed."""


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GovernanceConfigError(f"{field} must be a mapping")
    return value


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _non_empty_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise GovernanceConfigError(f"{field} must be a non-empty list")
    return [_non_empty_string(item, f"{field}[]") for item in value]


def _validate_accountability(value: object) -> dict[str, Any]:
    accountability = _mapping(value, "accountability")
    status = _non_empty_string(accountability.get("status"), "accountability.status")
    if status not in {"assigned", "unassigned"}:
        raise GovernanceConfigError("accountability.status must be assigned or unassigned")
    role = accountability.get("role")
    holder = accountability.get("holder")
    accepted_on = accountability.get("accepted_on")
    if status == "unassigned" and any(item is not None for item in (role, holder, accepted_on)):
        raise GovernanceConfigError(
            "accountability role, holder and accepted_on must be null while accountability is unassigned"
        )
    if status == "assigned":
        _non_empty_string(role, "accountability.role")
        _non_empty_string(holder, "accountability.holder")
        accepted = _non_empty_string(accepted_on, "accountability.accepted_on")
        try:
            date.fromisoformat(accepted)
        except ValueError as exc:
            raise GovernanceConfigError("accountability.accepted_on must be an ISO date") from exc
    if status == "unassigned":
        _non_empty_string(accountability.get("gap"), "accountability.gap")
    return accountability


def _validate_terminal_outcome(name: str, value: object) -> None:
    outcome = _mapping(value, f"completion_contract.terminal_outcomes.{name}")
    prefix = f"completion_contract.terminal_outcomes.{name}"
    _non_empty_string(outcome.get("predicate"), f"{prefix}.predicate")
    _non_empty_string(outcome.get("authoritative_state"), f"{prefix}.authoritative_state")
    _non_empty_string_list(outcome.get("required_evidence"), f"{prefix}.required_evidence")


def _validate_completion_contract(value: object) -> dict[str, Any]:
    contract = _mapping(value, "completion_contract")
    terminal = _mapping(contract.get("terminal_outcomes"), "completion_contract.terminal_outcomes")
    terminal_names = set(terminal)
    if "recovery_required" in terminal_names:
        raise GovernanceConfigError("recovery_required must never be terminal")
    if terminal_names != TERMINAL_OUTCOMES:
        expected = ", ".join(sorted(TERMINAL_OUTCOMES))
        raise GovernanceConfigError(f"completion_contract.terminal_outcomes must contain exactly: {expected}")
    for name, outcome in terminal.items():
        _validate_terminal_outcome(name, outcome)

    non_terminal = _mapping(
        contract.get("non_terminal_states"),
        "completion_contract.non_terminal_states",
    )
    missing_non_terminal = REQUIRED_NON_TERMINAL_STATES.difference(non_terminal)
    if missing_non_terminal:
        missing = ", ".join(sorted(missing_non_terminal))
        raise GovernanceConfigError(f"completion_contract.non_terminal_states is missing: {missing}")
    overlap = terminal_names.intersection(non_terminal)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise GovernanceConfigError(f"completion states cannot be both terminal and non-terminal: {names}")

    recovery = _mapping(
        non_terminal["recovery_required"],
        "completion_contract.non_terminal_states.recovery_required",
    )
    recovery_prefix = "completion_contract.non_terminal_states.recovery_required"
    _non_empty_string(recovery.get("predicate"), f"{recovery_prefix}.predicate")
    _non_empty_string(recovery.get("authoritative_state"), f"{recovery_prefix}.authoritative_state")
    _non_empty_string(recovery.get("required_action"), f"{recovery_prefix}.required_action")
    return contract


def load_process_governance(repo_root: str | Path) -> dict[str, Any]:
    """Load and validate the fixed process-governance contract for ``repo_root``."""

    root = Path(repo_root).resolve()
    path = _resolve_allowlisted_artifact(root, PROCESS_CONFIG_PATH, required=True)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise GovernanceConfigError(f"{PROCESS_CONFIG_PATH} is not valid YAML") from exc
    config = _mapping(payload, PROCESS_CONFIG_PATH)
    if config.get("schema_version") != SCHEMA_VERSION:
        raise GovernanceConfigError(f"schema_version must be {SCHEMA_VERSION}")

    process = _mapping(config.get("process"), "process")
    _non_empty_string(process.get("id"), "process.id")
    _non_empty_string(process.get("version"), "process.version")
    _non_empty_string(process.get("name"), "process.name")

    runtime_binding = _mapping(config.get("runtime_binding"), "runtime_binding")
    binding_status = _non_empty_string(runtime_binding.get("status"), "runtime_binding.status")
    if binding_status not in RUNTIME_BINDING_STATUSES:
        allowed = ", ".join(sorted(RUNTIME_BINDING_STATUSES))
        raise GovernanceConfigError(f"runtime_binding.status must be one of: {allowed}")

    instance = _mapping(config.get("instance_contract"), "instance_contract")
    for field in (
        "trigger",
        "work_object",
        "instance_key",
        "process_instance_id",
        "runtime_run_id",
        "attempt_id",
    ):
        _non_empty_string(instance.get(field), f"instance_contract.{field}")

    boundary = _mapping(config.get("boundary"), "boundary")
    for field in ("start", "result", "end"):
        _non_empty_string(boundary.get(field), f"boundary.{field}")
    _non_empty_string_list(boundary.get("excluded"), "boundary.excluded")

    _validate_accountability(config.get("accountability"))
    _validate_completion_contract(config.get("completion_contract"))
    return config


def _resolve_allowlisted_artifact(root: Path, relative_path: str, *, required: bool) -> Path | None:
    candidate = root / relative_path
    if candidate.is_symlink():
        raise GovernanceConfigError(f"allowlisted governance artifact cannot be a symlink: {relative_path}")
    if not candidate.exists():
        if required:
            raise GovernanceConfigError(f"required governance artifact is missing: {relative_path}")
        return None
    if not candidate.is_file():
        raise GovernanceConfigError(f"governance artifact is not a regular file: {relative_path}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise GovernanceConfigError(f"governance artifact escapes repository root: {relative_path}") from exc
    return resolved


def _artifact_fingerprint(root: Path, relative_path: str, *, required: bool) -> dict[str, Any] | None:
    path = _resolve_allowlisted_artifact(root, relative_path, required=required)
    if path is None:
        return None
    content = path.read_bytes()
    return {
        "path": relative_path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "required": required,
    }


def current_governance_version(repo_root: str | Path) -> dict[str, Any]:
    """Return the validated governance identity derived only from fixed artifacts.

    The artifact paths are constants in this module: configuration values cannot
    add arbitrary files, and environment or secret files are never inspected.
    """

    root = Path(repo_root).resolve()
    config = load_process_governance(root)
    artifacts: list[dict[str, Any]] = []
    for relative_path in REQUIRED_ARTIFACT_PATHS:
        artifact = _artifact_fingerprint(root, relative_path, required=True)
        assert artifact is not None
        artifacts.append(artifact)

    missing_optional: list[str] = []
    for relative_path in OPTIONAL_ARTIFACT_PATHS:
        artifact = _artifact_fingerprint(root, relative_path, required=False)
        if artifact is None:
            missing_optional.append(relative_path)
        else:
            artifacts.append(artifact)

    bundle_payload = [{"path": artifact["path"], "sha256": artifact["sha256"]} for artifact in artifacts]
    canonical = json.dumps(bundle_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    process = config["process"]
    accountability = config["accountability"]
    completion_contract = config["completion_contract"]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "allowlisted_governance_artifacts",
        "process_id": process["id"],
        "process_version": process["version"],
        "runtime_binding_status": config["runtime_binding"]["status"],
        "accountability": {
            "status": accountability["status"],
            "role": accountability.get("role"),
            "holder": accountability.get("holder"),
            "accepted_on": accountability.get("accepted_on"),
        },
        "completion_contract": {
            "terminal_outcomes": sorted(completion_contract["terminal_outcomes"]),
            "non_terminal_states": sorted(completion_contract["non_terminal_states"]),
        },
        "bundle_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "artifacts": artifacts,
        "missing_optional_artifacts": missing_optional,
    }
