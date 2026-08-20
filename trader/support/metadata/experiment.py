"""Stable, non-secret experiment identity for decision outcome cohorts.

This façade owns cohort validation and IDs. Execution-profile collection lives
in :mod:`experiment_profiles`, keeping ACPX/backend fingerprints independent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .experiment_profiles import model_profiles_from_backends
from .experiment_profiles.acpx import (  # noqa: F401
    _AGENT_ALIASES,
    _AGENT_EXEC_TRUE,
    _BUILT_IN_AGENT_ARGV,
    _EXACT_SEMVER,
    _acpx_configured_agents,
    _acpx_initial_cwd,
    _acpx_profile_fingerprint,
    _acpx_transport_fingerprint,
    _agent_adapter_fingerprint,
    _cached_npx_package_identity,
    _configured_agent_argv,
    _effective_agent_invocation,
    _exact_npm_package_spec,
    _node_package_bin_entrypoint,
    _profile_config_identity,
    _profile_config_path,
    _resolved_executable_identity,
)
from .experiment_profiles.backends import _provider_endpoint_fingerprint  # noqa: F401
from .experiment_profiles.common import _MODEL_PROFILE_FIELDS, _clean
from .experiment_profiles.common import (  # noqa: F401
    _canonical_fingerprint,
    _stable_file_sha256,
    _stable_json_object,
)
from .experiment_profiles.node_artifacts import (  # noqa: F401
    _NODE_LOCKFILE_NAMES,
    _RUNTIME_ARTIFACT_SUFFIXES,
    _acpx_package_fingerprint,
    _is_runtime_artifact,
    _nearest_node_lockfiles,
    _node_package_for_entrypoint,
    _node_package_graph_fingerprint,
    _resolve_node_dependency,
    _runtime_tree_fingerprint,
)

SCHEMA_VERSION = 3
ID_PREFIX = "exp:v3:"
V2_ID_PREFIX = "exp:v2:"
LEGACY_ID_PREFIX = "exp:v1:"

_PRESET_MARKER = re.compile(r"^# >>> casys:model-preset=(?P<name>[\w.-]+) >>>$")
_NUMERIC_RISK_FIELDS = (
    "max_position_value",
    "max_gross_exposure",
    "max_order_value",
    "min_equity",
    "max_risk_per_trade_pct",
    "min_trade_confidence",
    "full_risk_confidence",
)
_BOOLEAN_RISK_FIELDS = ("confidence_gate_enabled", "require_hard_stop")
_RISK_FIELDS = frozenset((*_NUMERIC_RISK_FIELDS, *_BOOLEAN_RISK_FIELDS))
_POSITIVE_RISK_FIELDS = frozenset(
    ("max_position_value", "max_gross_exposure", "max_order_value", "max_risk_per_trade_pct")
)
_CONFIDENCE_RISK_FIELDS = frozenset(("min_trade_confidence", "full_risk_confidence"))


def commission_model_identity(value: object | None) -> str:
    """Return a stable, non-sensitive name for the active fee policy."""
    if value is None:
        return "none"
    class_name = type(value).__name__
    known = {"IbkrCommissionModel": "ibkr", "NoCommissionModel": "none"}
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


def _validated_risk(risk_policy: object) -> tuple[dict[str, object], list[str]]:
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
        number = float(raw)
        if not math.isfinite(number):
            issues.append(f"risk.{field}:non_finite")
            continue
        if field in _POSITIVE_RISK_FIELDS and number <= 0.0:
            issues.append(f"risk.{field}:must_be_positive")
            continue
        if field == "min_equity" and number < 0.0:
            issues.append("risk.min_equity:must_be_non_negative")
            continue
        if field == "max_risk_per_trade_pct" and number > 1.0:
            issues.append("risk.max_risk_per_trade_pct:out_of_range")
            continue
        if field in _CONFIDENCE_RISK_FIELDS and not 0.0 <= number <= 1.0:
            issues.append(f"risk.{field}:out_of_range")
            continue
        risk[field] = number
    for field in _BOOLEAN_RISK_FIELDS:
        if field not in risk_policy:
            continue
        raw = risk_policy[field]
        if not isinstance(raw, bool):
            issues.append(f"risk.{field}:invalid_type")
            continue
        risk[field] = raw
    low, high = risk.get("min_trade_confidence"), risk.get("full_risk_confidence")
    if isinstance(low, float) and isinstance(high, float) and low > high:
        issues.append("risk.confidence_thresholds:min_exceeds_full")
    return risk, issues


def _validated_model_profiles(
    value: object, *, report_profile_issues: bool = True, selected_provider: str | None = None
) -> tuple[dict[str, dict[str, object]], list[str]]:
    if not isinstance(value, Mapping):
        return {}, ["model.profiles:missing_or_invalid"]
    profiles: dict[str, dict[str, object]] = {}
    issues: list[str] = []

    def report(provider: str) -> bool:
        return report_profile_issues and (selected_provider is None or selected_provider == provider)

    for raw_provider, raw_profile in value.items():
        provider = _clean(raw_provider)
        if provider is None or not isinstance(raw_profile, Mapping):
            if report_profile_issues and selected_provider is None:
                issues.append("model.profile:invalid")
            continue
        if {str(key) for key in raw_profile} != _MODEL_PROFILE_FIELDS:
            if report(provider):
                issues.append(f"model.profile.{provider}:invalid_fields")
            continue
        configured_model = _clean(raw_profile.get("configured_model"))
        transport = _clean(raw_profile.get("transport"))
        fingerprint = _clean(raw_profile.get("profile_fingerprint"))
        agent, effort = raw_profile.get("agent"), raw_profile.get("reasoning_effort")
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
    profiles, profile_issues = _validated_model_profiles(model_profiles, report_profile_issues=False)
    issues = [str(issue) for issue in runtime_issues]
    issues.extend(risk_issues)
    issues.extend(profile_issues)
    git_commit = str(code_version.get("git_commit") or "").strip() or None
    if git_commit is None:
        issues.append("git_commit:missing")
    worktree_dirty = code_version.get("git_dirty")
    tracked_dirty = code_version.get("git_tracked_dirty")
    if not isinstance(tracked_dirty, bool):
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
        "git_worktree_dirty": worktree_dirty if isinstance(worktree_dirty, bool) else None,
        "git_tracked_dirty": tracked_dirty,
        "model_preset": preset,
        "commission_model": commission,
        "risk": risk,
        "model_profiles": profiles,
        "issues": list(dict.fromkeys(issues)),
    }


def decision_experiment(context: Mapping[str, object] | None, *, provider: object, model: object) -> dict[str, Any]:
    """Resolve one decision's versioned cohort descriptor and stable hash."""
    source = context if isinstance(context, Mapping) else {}
    issues = [str(value) for value in source.get("issues", [])] if isinstance(source.get("issues"), list) else []
    provider_name, model_name = str(provider or "").strip() or None, str(model or "").strip() or None
    if provider_name is None:
        issues.append("model.provider:missing")
    if model_name is None:
        issues.append("model.model:missing")
    risk_values, risk_issues = _validated_risk(source.get("risk"))
    issues.extend(risk_issues)
    profiles, profile_issues = _validated_model_profiles(source.get("model_profiles"), selected_provider=provider_name)
    issues.extend(profile_issues)
    profile = profiles.get(provider_name or "")
    if profile is None:
        issues.append(f"model.profile.{provider_name or 'unknown'}:missing")
        execution_profile: dict[str, object] = {}
    else:
        if profile.get("configured_model") != model_name:
            issues.append("model.profile:configured_model_mismatch")
        execution_profile = {key: profile[key] for key in _MODEL_PROFILE_FIELDS if key != "configured_model"}
    components = {
        "git_commit": source.get("git_commit"),
        "git_tracked_dirty": source.get("git_tracked_dirty"),
        "model": {
            "provider": provider_name,
            "model": model_name,
            "preset": source.get("model_preset"),
            "execution_profile": execution_profile,
        },
        "execution": {"commission_model": source.get("commission_model")},
        "risk": risk_values,
    }
    unique_issues = list(dict.fromkeys(issues))
    experiment_id = _experiment_id(components) if not unique_issues else None
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "components": components,
        "issues": unique_issues,
        "decision_grade": experiment_id is not None and source.get("git_tracked_dirty") is False,
    }


def _experiment_id(components: Mapping[str, object], *, prefix: str = ID_PREFIX) -> str:
    canonical = json.dumps(
        components, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
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
    if _clean(components.get("git_commit")) is None or components.get("git_tracked_dirty") is not False:
        return False
    model, execution = components.get("model"), components.get("execution")
    if not isinstance(model, Mapping) or not isinstance(execution, Mapping):
        return False
    expected_model_fields = {"provider", "model", "preset"}
    if schema_version >= 2:
        expected_model_fields.add("execution_profile")
    if {str(key) for key in model} != expected_model_fields:
        return False
    if not all(_clean(model.get(key)) is not None for key in ("provider", "model", "preset")):
        return False
    if {str(key) for key in execution} != {"commission_model"} or _clean(execution.get("commission_model")) is None:
        return False
    if schema_version >= 2:
        profile = model.get("execution_profile")
        if not isinstance(profile, Mapping) or {str(key) for key in profile} != _MODEL_PROFILE_FIELDS - {
            "configured_model"
        }:
            return False
        if _clean(profile.get("transport")) is None or _clean(profile.get("profile_fingerprint")) is None:
            return False
        if profile.get("transport") == "acpx" and (
            _clean(profile.get("agent")) is None or _clean(profile.get("reasoning_effort")) is None
        ):
            return False
    return True


def inherited_experiment(value: object) -> dict[str, Any] | None:
    """Accept a complete experiment descriptor propagated by an armed plan."""
    if not isinstance(value, Mapping):
        return None
    experiment_id, components = value.get("experiment_id"), value.get("components")
    if not isinstance(experiment_id, str) or not isinstance(components, Mapping):
        return None
    version_and_prefix = _id_schema(experiment_id)
    if version_and_prefix is None:
        return None
    schema_version, prefix = version_and_prefix
    try:
        expected_id = _experiment_id(components, prefix=prefix)
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(experiment_id, expected_id):
        return None
    if value.get("schema_version") is not None and value.get("schema_version") != schema_version:
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
