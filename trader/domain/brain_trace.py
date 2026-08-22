"""Stable Brain trade-trace identity. Fail closed; never invent missing state."""

from __future__ import annotations

from typing import Any

__all__ = [
    "episode_id",
    "identity_text",
    "mandate_id_from_ref",
    "observation_ref",
    "post_effect_snapshot_ref",
    "unique_identity",
]


def identity_text(value: Any) -> str | None:
    """Normalize a durable identifier. Zero, blank, and booleans are missing."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        return str(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text == "0":
        return None
    return text


def unique_identity(*values: Any) -> str | None:
    """Return the single non-empty identity, or None when missing or conflicting."""

    found = {item for value in values if (item := identity_text(value)) is not None}
    if len(found) != 1:
        return None
    return next(iter(found))


def episode_id(
    *,
    task_id: Any,
    process_instance_id: Any,
    decision_source: Any = None,
) -> str | None:
    """Episode key for one Trader decision task. Infra HOLDs are not Brain episodes."""

    source = identity_text(decision_source)
    if source is not None and source.lower() == "infra":
        return None
    task = identity_text(task_id)
    process = identity_text(process_instance_id)
    if task is None or process is None:
        return None
    return f"trader-episode:task:{task}:process:{process}"


def observation_ref(*, ts: Any, source: Any = None) -> dict[str, str | None]:
    observed_at = identity_text(ts)
    if observed_at is None:
        return {"status": "unavailable", "ts": None, "source": None}
    return {
        "status": "available",
        "ts": observed_at,
        "source": identity_text(source),
    }


def post_effect_snapshot_ref(*, snapshot_id: Any) -> dict[str, str | None]:
    resolved = identity_text(snapshot_id)
    if resolved is None:
        return {"status": "unavailable", "snapshot_id": None}
    return {"status": "available", "snapshot_id": resolved}


def mandate_id_from_ref(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return identity_text(value.get("mandate_id"))
