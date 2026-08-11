"""Identités et événements immuables d'un épisode de processus L2."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

TERMINAL_RESULTS = frozenset({"completed", "failed", "cancelled", "escalated"})


def new_process_instance_id() -> str:
    """Retourne un identifiant opaque pour une instance métier L2."""

    return str(uuid4())


def new_attempt_id() -> str:
    """Retourne un identifiant opaque pour une tentative de traitement L2."""

    return str(uuid4())


def new_runtime_run_id() -> str:
    """Retourne un identifiant opaque pour un démarrage du runtime."""

    return str(uuid4())


def new_process_event_id() -> str:
    """Retourne un identifiant opaque utilisable comme clé d'idempotence."""

    return str(uuid4())


@dataclass(frozen=True)
class ProcessIdentity:
    """Corrélation minimale d'une tentative avec son instance et son runtime."""

    process_instance_id: str
    attempt_id: str
    runtime_run_id: str

    @classmethod
    def create(cls, *, runtime_run_id: str | None = None) -> ProcessIdentity:
        return cls(
            process_instance_id=new_process_instance_id(),
            attempt_id=new_attempt_id(),
            runtime_run_id=runtime_run_id or new_runtime_run_id(),
        )

    def next_attempt(self, *, runtime_run_id: str | None = None) -> ProcessIdentity:
        """Conserve l'instance, renouvelle la tentative et éventuellement le runtime."""

        return ProcessIdentity(
            process_instance_id=self.process_instance_id,
            attempt_id=new_attempt_id(),
            runtime_run_id=runtime_run_id or self.runtime_run_id,
        )

    def __post_init__(self) -> None:
        for name in ("process_instance_id", "attempt_id", "runtime_run_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))


@dataclass(frozen=True)
class ProcessEvent:
    """Événement append-only d'une instance de processus.

    ``event_id`` est la clé d'idempotence. Les champs JSON sont copiés à la
    construction afin qu'une mutation ultérieure de l'appelant ne change pas
    silencieusement l'événement à persister.
    """

    event_id: str
    process_type: str
    process_version: str
    process_instance_id: str
    runtime_run_id: str
    work_object_type: str
    work_object_key: str
    event_type: str
    ts: str
    attempt_id: str | None = None
    caused_by: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    terminal_result: str | None = None
    outcome_code: str | None = None
    effect_status: str | None = None
    effect_refs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    version_pins: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        process_type: str,
        process_version: str,
        process_instance_id: str,
        runtime_run_id: str,
        work_object_type: str,
        work_object_key: str,
        event_type: str,
        attempt_id: str | None = None,
        caused_by: Sequence[Mapping[str, Any]] = (),
        terminal_result: str | None = None,
        outcome_code: str | None = None,
        effect_status: str | None = None,
        effect_refs: Sequence[Mapping[str, Any]] = (),
        version_pins: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        ts: str | None = None,
    ) -> ProcessEvent:
        return cls(
            event_id=event_id or new_process_event_id(),
            process_type=process_type,
            process_version=process_version,
            process_instance_id=process_instance_id,
            attempt_id=attempt_id,
            runtime_run_id=runtime_run_id,
            work_object_type=work_object_type,
            work_object_key=work_object_key,
            event_type=event_type,
            ts=ts or datetime.now(timezone.utc).isoformat(),
            caused_by=tuple(dict(item) for item in caused_by),
            terminal_result=terminal_result,
            outcome_code=outcome_code,
            effect_status=effect_status,
            effect_refs=tuple(dict(item) for item in effect_refs),
            version_pins=None if version_pins is None else dict(version_pins),
        )

    def __post_init__(self) -> None:
        required = (
            "event_id",
            "process_type",
            "process_version",
            "process_instance_id",
            "runtime_run_id",
            "work_object_type",
            "work_object_key",
            "event_type",
        )
        for name in required:
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if self.attempt_id is not None:
            object.__setattr__(self, "attempt_id", _required_text(self.attempt_id, "attempt_id"))

        object.__setattr__(self, "ts", _canonical_timestamp(self.ts))
        object.__setattr__(self, "caused_by", _json_object_tuple(self.caused_by, "caused_by"))
        object.__setattr__(self, "effect_refs", _json_object_tuple(self.effect_refs, "effect_refs"))
        if self.version_pins is not None:
            pins = _json_copy(dict(self.version_pins), "version_pins")
            object.__setattr__(self, "version_pins", pins)

        if self.terminal_result is not None:
            result = _required_text(self.terminal_result, "terminal_result")
            if result not in TERMINAL_RESULTS:
                allowed = ", ".join(sorted(TERMINAL_RESULTS))
                raise ValueError(f"terminal_result must be one of: {allowed}")
            object.__setattr__(self, "terminal_result", result)
        if self.event_type == "instance_closed" and self.terminal_result is None:
            raise ValueError("instance_closed requires terminal_result")
        if self.event_type != "instance_closed" and self.terminal_result is not None:
            raise ValueError("terminal_result is only valid on instance_closed")


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _canonical_timestamp(value: str) -> str:
    raw = _required_text(value, "ts")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ts must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("ts must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _json_object_tuple(
    values: Sequence[Mapping[str, Any]],
    field_name: str,
) -> tuple[dict[str, Any], ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be a sequence of objects")
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise TypeError(f"{field_name} entries must be objects")
        item = _json_copy(dict(value), field_name)
        result.append(item)
    return tuple(result)


def _json_copy(value: Any, field_name: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain finite JSON values") from exc
    return json.loads(encoded)


__all__ = [
    "ProcessEvent",
    "ProcessIdentity",
    "TERMINAL_RESULTS",
    "new_attempt_id",
    "new_process_event_id",
    "new_process_instance_id",
    "new_runtime_run_id",
]
