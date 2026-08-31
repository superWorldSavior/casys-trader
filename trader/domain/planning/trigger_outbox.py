"""Pure contract for durable indicator-trigger deliveries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


INDICATOR_TRIGGER_OUTBOX_STATE_KEY = "indicator_trigger_outbox"
TRIGGER_OUTBOX_ID_FIELD = "trigger_outbox_id"


def normalize_trigger_outbox(value: object) -> dict[str, dict[str, Any]]:
    """Validate an outbox mapping without silently discarding pending work."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("indicator_trigger_outbox must be a mapping")
    normalized: dict[str, dict[str, Any]] = {}
    for watch_id, payload in value.items():
        if not isinstance(watch_id, str) or not watch_id:
            raise ValueError("indicator_trigger_outbox watch_id must be non-empty")
        if not isinstance(payload, Mapping):
            raise ValueError("indicator_trigger_outbox payload must be a mapping")
        item = dict(payload)
        if str(item.get("watch_id") or "") != watch_id:
            raise ValueError("indicator_trigger_outbox watch_id mismatch")
        if str(item.get(TRIGGER_OUTBOX_ID_FIELD) or "") != watch_id:
            raise ValueError("indicator_trigger_outbox id mismatch")
        normalized[watch_id] = item
    return normalized


def prune_expired_trigger_outbox(
    value: object,
    *,
    now: datetime,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Split live deliveries from expired IDs using their authoritative TTL."""
    normalized = normalize_trigger_outbox(value)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    active: dict[str, dict[str, Any]] = {}
    expired: list[str] = []
    for watch_id, payload in normalized.items():
        raw_expires_at = payload.get("expires_at")
        if not raw_expires_at:
            active[watch_id] = payload
            continue
        try:
            expires_at = datetime.fromisoformat(
                str(raw_expires_at).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("indicator_trigger_outbox expires_at invalid") from exc
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)
        if expires_at <= now:
            expired.append(watch_id)
        else:
            active[watch_id] = payload
    return active, expired


def build_trigger_delivery(
    *,
    watch: Mapping[str, Any],
    watch_id: str,
    symbol: str,
    closed_bar_key: str | None,
    claimed_at: str,
    trigger_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the authoritative payload persisted by an atomic trigger claim."""
    payload = dict(trigger_payload)
    payload.update(
        {
            TRIGGER_OUTBOX_ID_FIELD: watch_id,
            "watch_id": watch_id,
            "symbol": symbol,
            "on_trigger": str(watch.get("on_trigger") or "WAKE"),
            "claimed_at": claimed_at,
            "expires_at": watch.get("expires_at"),
        }
    )
    if closed_bar_key is None:
        payload.pop("closed_bar_key", None)
    else:
        payload["closed_bar_key"] = closed_bar_key
    if "order" in watch:
        payload["order"] = watch["order"]
    else:
        payload.pop("order", None)
    return payload


__all__ = [
    "INDICATOR_TRIGGER_OUTBOX_STATE_KEY",
    "TRIGGER_OUTBOX_ID_FIELD",
    "build_trigger_delivery",
    "normalize_trigger_outbox",
    "prune_expired_trigger_outbox",
]
