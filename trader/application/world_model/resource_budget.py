"""Typed last-resort World Model resource budget: load, decide, rate-limit.

The versioned config is the operator surface. Missing or invalid config falls
back to conservative defaults so the guard stays on. This module does not
import infrastructure, runtime, or reporting, and it never deletes evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from trader.application.world_model.resource_ports import WorldResourceProbe
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_resource import (
    REASON_PROBE_ERROR,
    WORLD_RESOURCE_BUDGET_SCHEMA,
    WorldResourceBudget,
    WorldResourceDecision,
    decide_world_resource_budget,
)


WORLD_SHADOW_RESOURCE_BUDGET_CONFIG_NAME = "world_shadow_resource_budget.yaml"
_AUTHORITY = "shadow_only"
_DECISION_EFFECT = "none"
_RECOMMENDATION = "NO_GO"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool")
    return value


def _config_path(config_dir: str | Path) -> Path:
    path = Path(config_dir)
    if path.is_file():
        return path
    return path / WORLD_SHADOW_RESOURCE_BUDGET_CONFIG_NAME


def _parse_world_shadow_resource_budget(path: Path) -> WorldResourceBudget:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("world_shadow_resource_budget.yaml must be a mapping")
    hashed = {str(key): value for key, value in payload.items() if key != "content_sha256"}
    claimed = _required_text(payload.get("content_sha256"), "content_sha256")
    digest = canonical_sha256(hashed)
    if digest != claimed:
        raise ValueError("content_sha256 does not match the canonical World resource-budget config")
    schema = _required_text(payload.get("schema_version"), "schema_version")
    if schema != WORLD_RESOURCE_BUDGET_SCHEMA:
        raise ValueError(f"schema_version must be {WORLD_RESOURCE_BUDGET_SCHEMA}")
    if _required_text(payload.get("authority"), "authority") != _AUTHORITY:
        raise ValueError("authority must be shadow_only")
    if _required_text(payload.get("decision_effect"), "decision_effect") != _DECISION_EFFECT:
        raise ValueError("decision_effect must be none")
    if _required_text(payload.get("recommendation"), "recommendation") != _RECOMMENDATION:
        raise ValueError("recommendation must be NO_GO")
    if _required_bool(payload.get("causal_claim"), "causal_claim"):
        raise ValueError("causal_claim must be false")
    if _required_bool(payload.get("pnl_claim"), "pnl_claim"):
        raise ValueError("pnl_claim must be false")
    return WorldResourceBudget(
        schema_version=schema,
        max_db_bytes=payload.get("max_db_bytes"),
        min_free_bytes=payload.get("min_free_bytes"),
        warn_interval_seconds=payload.get("warn_interval_seconds"),
        content_sha256=digest,
    )


def load_world_shadow_resource_budget(config_dir: str | Path) -> WorldResourceBudget:
    """Load the versioned budget, or conservative defaults. Never raises."""

    path = _config_path(config_dir)
    try:
        if not path.is_file():
            return WorldResourceBudget.conservative_defaults()
        return _parse_world_shadow_resource_budget(path)
    except Exception:  # noqa: BLE001 - invalid config cannot disable the last-resort guard
        return WorldResourceBudget.conservative_defaults()


@dataclass(frozen=True)
class WorldResourceEvaluation:
    decision: WorldResourceDecision
    emit_warning: bool

    def to_status(self) -> dict[str, Any]:
        payload = self.decision.to_dict()
        payload["warning_emitted"] = self.emit_warning
        return payload


class WorldResourceBudgetGuard:
    """One probe + one decision per write batch, with rate-limited warnings."""

    def __init__(
        self,
        *,
        budget: WorldResourceBudget,
        probe: WorldResourceProbe,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.budget = budget
        self.probe = probe
        self._clock = clock
        self._last_warning_at: datetime | None = None
        self._last_decision: WorldResourceDecision | None = None

    def evaluate(self, *, now: datetime | None = None) -> WorldResourceEvaluation:
        if now is not None:
            current = _utc(now)
        elif self._clock is not None:
            current = _utc(self._clock())
        else:
            current = datetime.now(timezone.utc)
        try:
            usage = self.probe.measure()
        except Exception:  # noqa: BLE001 - unreadable disk is fail-safe skip for shadow writes
            decision = WorldResourceDecision(
                status="skipped",
                reason=REASON_PROBE_ERROR,
                budget=self.budget,
                usage=None,
                breaches=(REASON_PROBE_ERROR,),
            )
        else:
            decision = decide_world_resource_budget(self.budget, usage)
        emit = False
        if not decision.allowed:
            if self._last_warning_at is None:
                emit = True
            elif (current - self._last_warning_at).total_seconds() >= self.budget.warn_interval_seconds:
                emit = True
            if emit:
                self._last_warning_at = current
        self._last_decision = decision
        return WorldResourceEvaluation(decision=decision, emit_warning=emit)


__all__ = [
    "WORLD_SHADOW_RESOURCE_BUDGET_CONFIG_NAME",
    "WorldResourceBudgetGuard",
    "WorldResourceEvaluation",
    "load_world_shadow_resource_budget",
]
