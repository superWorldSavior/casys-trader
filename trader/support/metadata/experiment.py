"""Stable, non-secret experiment identity for decision outcome cohorts."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ID_PREFIX = "exp:v1:"

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


def build_experiment_context(
    *,
    code_version: Mapping[str, object],
    model_preset: str | None,
    risk_policy: Mapping[str, object],
    commission_model: str | None,
) -> dict[str, Any]:
    """Build the cycle-stable inputs needed for per-decision identity."""

    risk: dict[str, object] = {}
    issues: list[str] = []
    for field in _NUMERIC_RISK_FIELDS:
        raw = risk_policy.get(field)
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            issues.append(f"risk.{field}:missing_or_invalid")
            continue
        if not math.isfinite(value):
            issues.append(f"risk.{field}:non_finite")
            continue
        risk[field] = value
    for field in _BOOLEAN_RISK_FIELDS:
        raw = risk_policy.get(field)
        if not isinstance(raw, bool):
            issues.append(f"risk.{field}:missing_or_invalid")
            continue
        risk[field] = raw

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
        "issues": issues,
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

    risk = source.get("risk")
    risk_values = dict(risk) if isinstance(risk, Mapping) else {}
    if len(risk_values) != len(_NUMERIC_RISK_FIELDS) + len(_BOOLEAN_RISK_FIELDS):
        issues.append("risk:incomplete")
    components = {
        "git_commit": source.get("git_commit"),
        "git_tracked_dirty": source.get("git_tracked_dirty"),
        "model": {
            "provider": provider_name,
            "model": model_name,
            "preset": source.get("model_preset"),
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


def _experiment_id(components: Mapping[str, object]) -> str:
    canonical = json.dumps(
        components,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return ID_PREFIX + hashlib.sha256(canonical).hexdigest()


def inherited_experiment(value: object) -> dict[str, Any] | None:
    """Accept a complete experiment descriptor propagated by an armed plan."""

    if not isinstance(value, Mapping):
        return None
    experiment_id = value.get("experiment_id")
    components = value.get("components")
    if not isinstance(experiment_id, str) or not experiment_id.startswith(ID_PREFIX):
        return None
    if not isinstance(components, Mapping):
        return None
    try:
        expected_id = _experiment_id(components)
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(experiment_id, expected_id):
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "components": dict(components),
        "issues": [],
        "decision_grade": value.get("decision_grade") is True,
    }


__all__ = [
    "ID_PREFIX",
    "SCHEMA_VERSION",
    "active_model_preset",
    "build_experiment_context",
    "commission_model_identity",
    "decision_experiment",
    "inherited_experiment",
]
