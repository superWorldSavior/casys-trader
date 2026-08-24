"""Shadow-local last-resort resource budget for World Model writes.

Stdlib-only. This policy never deletes or rewrites historical evidence, never
stops a daemon, and never grants Trader decision effect. A breached budget
skips the next World Model write batch; trading continues fail-open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WORLD_RESOURCE_BUDGET_SCHEMA = "world_shadow_resource_budget.v1"
REASON_WITHIN_BUDGET = "within_budget"
REASON_DB_SIZE_EXCEEDED = "db_size_exceeded"
REASON_FREE_SPACE_BELOW_RESERVE = "free_space_below_reserve"
REASON_PROBE_ERROR = "probe_error"

# Conservative one-week last-resort on a host currently around 7 GiB free.
# Polluted capture grew ~2 MiB/min; 2 GiB caps that to ~17 h, and 3 GiB stays
# reserved for the OS, logs, and Trader state.
DEFAULT_MAX_DB_BYTES = 2 * 1024 ** 3
DEFAULT_MIN_FREE_BYTES = 3 * 1024 ** 3
DEFAULT_WARN_INTERVAL_SECONDS = 300
_DECISION_STATUSES = frozenset({"allowed", "skipped"})


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_int(value: Any, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an int")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class WorldResourceBudget:
    """Operator-configured last-resort caps for ``world_model.db`` writes."""

    schema_version: str
    max_db_bytes: int
    min_free_bytes: int
    warn_interval_seconds: int
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema = _required_text(self.schema_version, "schema_version")
        if schema != WORLD_RESOURCE_BUDGET_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_RESOURCE_BUDGET_SCHEMA}")
        object.__setattr__(self, "schema_version", schema)
        object.__setattr__(self, "max_db_bytes", _required_int(self.max_db_bytes, "max_db_bytes", minimum=1))
        object.__setattr__(
            self,
            "min_free_bytes",
            _required_int(self.min_free_bytes, "min_free_bytes", minimum=0),
        )
        object.__setattr__(
            self,
            "warn_interval_seconds",
            _required_int(self.warn_interval_seconds, "warn_interval_seconds", minimum=1),
        )
        digest = self.content_sha256
        if digest is not None:
            object.__setattr__(self, "content_sha256", _required_text(digest, "content_sha256"))

    @classmethod
    def conservative_defaults(cls) -> WorldResourceBudget:
        return cls(
            schema_version=WORLD_RESOURCE_BUDGET_SCHEMA,
            max_db_bytes=DEFAULT_MAX_DB_BYTES,
            min_free_bytes=DEFAULT_MIN_FREE_BYTES,
            warn_interval_seconds=DEFAULT_WARN_INTERVAL_SECONDS,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "max_db_bytes": self.max_db_bytes,
            "min_free_bytes": self.min_free_bytes,
            "warn_interval_seconds": self.warn_interval_seconds,
        }
        if self.content_sha256 is not None:
            payload["content_sha256"] = self.content_sha256
        return payload


@dataclass(frozen=True)
class WorldResourceUsage:
    """One filesystem snapshot for a World Model write-batch decision."""

    logical_bytes: int
    on_disk_bytes: int
    free_bytes: int
    store_exists: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_bytes", _required_int(self.logical_bytes, "logical_bytes", minimum=0))
        object.__setattr__(self, "on_disk_bytes", _required_int(self.on_disk_bytes, "on_disk_bytes", minimum=0))
        object.__setattr__(self, "free_bytes", _required_int(self.free_bytes, "free_bytes", minimum=0))
        if not isinstance(self.store_exists, bool):
            raise TypeError("store_exists must be a bool")


@dataclass(frozen=True)
class WorldResourceDecision:
    """Allow or skip the next World Model capture/training batch."""

    status: str
    reason: str
    budget: WorldResourceBudget
    usage: WorldResourceUsage | None = None
    breaches: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in _DECISION_STATUSES:
            raise ValueError("status must be allowed or skipped")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        if not isinstance(self.breaches, tuple):
            raise TypeError("breaches must be a tuple")

    @property
    def allowed(self) -> bool:
        return self.status == "allowed"

    def to_dict(self) -> dict[str, Any]:
        usage = self.usage
        return {
            "status": self.status,
            "reason": self.reason,
            "breaches": list(self.breaches),
            "authority": "shadow_only",
            "decision_effect": "none",
            "recommendation": "NO_GO",
            "causal_claim": False,
            "pnl_claim": False,
            "schema_version": self.budget.schema_version,
            "max_db_bytes": self.budget.max_db_bytes,
            "min_free_bytes": self.budget.min_free_bytes,
            "warn_interval_seconds": self.budget.warn_interval_seconds,
            "logical_bytes": None if usage is None else usage.logical_bytes,
            "on_disk_bytes": None if usage is None else usage.on_disk_bytes,
            "free_bytes": None if usage is None else usage.free_bytes,
            "store_exists": None if usage is None else usage.store_exists,
        }


def decide_world_resource_budget(
    budget: WorldResourceBudget,
    usage: WorldResourceUsage,
) -> WorldResourceDecision:
    """Return one allow/skip decision for a write batch. No I/O."""

    if not isinstance(budget, WorldResourceBudget):
        raise TypeError("budget must be WorldResourceBudget")
    if not isinstance(usage, WorldResourceUsage):
        raise TypeError("usage must be WorldResourceUsage")
    breaches: list[str] = []
    if max(usage.logical_bytes, usage.on_disk_bytes) >= budget.max_db_bytes:
        breaches.append(REASON_DB_SIZE_EXCEEDED)
    if usage.free_bytes < budget.min_free_bytes:
        breaches.append(REASON_FREE_SPACE_BELOW_RESERVE)
    if not breaches:
        return WorldResourceDecision(
            status="allowed",
            reason=REASON_WITHIN_BUDGET,
            budget=budget,
            usage=usage,
            breaches=(),
        )
    return WorldResourceDecision(
        status="skipped",
        reason=breaches[0],
        budget=budget,
        usage=usage,
        breaches=tuple(breaches),
    )


__all__ = [
    "DEFAULT_MAX_DB_BYTES",
    "DEFAULT_MIN_FREE_BYTES",
    "DEFAULT_WARN_INTERVAL_SECONDS",
    "REASON_DB_SIZE_EXCEEDED",
    "REASON_FREE_SPACE_BELOW_RESERVE",
    "REASON_PROBE_ERROR",
    "REASON_WITHIN_BUDGET",
    "WORLD_RESOURCE_BUDGET_SCHEMA",
    "WorldResourceBudget",
    "WorldResourceDecision",
    "WorldResourceUsage",
    "decide_world_resource_budget",
]
