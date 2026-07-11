"""Pure contract for the cross-region universe posture (PM global level).

`GlobalUniversePosture` is the decision artefact produced once per cycle by the
universe role acting *globally*: it ranks venues and families and sets a gross/net
stance. It is advisory (a frame, never a quota, an allocation or an order) and is
injected identically into every regional universe pass so no pass depends on
another pass's decision (see the 2026-07-11 mandate-consolidation design).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_RATIONALE_CHARS = 600
DEFAULT_MAX_PRIORITY_FAMILIES = 8
DEFAULT_MAX_SOURCE_REFS = 8

VENUE_POSTURES = ("favor", "selective", "watch", "avoid")
GROSS_MODES = ("normal", "cautious", "risk_off")
NET_BIASES = ("long", "short", "neutral")


def _clean_text(value: Any, *, max_chars: int | None = None) -> str:
    text = str(value or "").strip()
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars]
    return text


def _enum(value: Any, *, allowed: tuple[str, ...], default: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in allowed else default


def _string_tuple(value: Any, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    cleaned: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return tuple(cleaned)


@dataclass(frozen=True)
class GlobalUniversePosture:
    """Bounded cross-region stance decided once by the global universe role."""

    as_of: str
    venue_posture: Mapping[str, str] = field(default_factory=dict)
    family_priority: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    gross_mode: str = "normal"
    net_bias: str = "neutral"
    rationale: str = ""
    valid_until: str | None = None
    source_refs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Any) -> "GlobalUniversePosture | None":
        if not isinstance(payload, Mapping):
            return None
        raw_venues = payload.get("venue_posture")
        venue_posture: dict[str, str] = {}
        if isinstance(raw_venues, Mapping):
            for venue, posture in raw_venues.items():
                key = str(venue or "").strip()
                if key:
                    venue_posture[key] = _enum(posture, allowed=VENUE_POSTURES, default="watch")
        raw_priority = payload.get("family_priority")
        family_priority: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_priority, Mapping):
            for bucket in ("favored", "deprioritized"):
                family_priority[bucket] = _string_tuple(
                    raw_priority.get(bucket), limit=DEFAULT_MAX_PRIORITY_FAMILIES
                )
        return cls(
            as_of=_clean_text(payload.get("as_of")),
            venue_posture=venue_posture,
            family_priority=family_priority,
            gross_mode=_enum(payload.get("gross_mode"), allowed=GROSS_MODES, default="normal"),
            net_bias=_enum(payload.get("net_bias"), allowed=NET_BIASES, default="neutral"),
            rationale=_clean_text(payload.get("rationale"), max_chars=DEFAULT_MAX_RATIONALE_CHARS),
            valid_until=(
                _clean_text(payload.get("valid_until")) or None
                if payload.get("valid_until") is not None
                else None
            ),
            source_refs=_string_tuple(payload.get("source_refs"), limit=DEFAULT_MAX_SOURCE_REFS),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "as_of": self.as_of,
            "valid_until": self.valid_until,
            "venue_posture": dict(self.venue_posture),
            "family_priority": {
                bucket: list(families) for bucket, families in self.family_priority.items()
            },
            "gross_mode": self.gross_mode,
            "net_bias": self.net_bias,
            "rationale": self.rationale,
            "source_refs": list(self.source_refs),
        }


__all__ = [
    "GlobalUniversePosture",
    "VENUE_POSTURES",
    "GROSS_MODES",
    "NET_BIASES",
]
