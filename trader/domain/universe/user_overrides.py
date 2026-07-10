"""Pure pin/ban policy for the configured trading universe."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UserOverrides:
    """User pin/ban intent; one symbol cannot be active in both sets."""

    pin: tuple[str, ...] = field(default=())
    ban: tuple[str, ...] = field(default=())

    def status_of(self, symbol: str) -> str | None:
        """Return ``pinned``, ``banned`` or ``None`` for one symbol."""

        if symbol in self.pin:
            return "pinned"
        if symbol in self.ban:
            return "banned"
        return None


def normalize_override_symbols(raw: object) -> tuple[str, ...]:
    """Normalize a user-provided symbol sequence with stable deduplication."""

    if not isinstance(raw, (list, tuple)):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        symbol = str(item or "").strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return tuple(result)


def normalized_user_overrides(
    *,
    pin: object = (),
    ban: object = (),
) -> UserOverrides:
    """Build overrides while enforcing stable order and pin precedence."""

    normalized_pin = normalize_override_symbols(pin)
    normalized_ban = tuple(
        symbol
        for symbol in normalize_override_symbols(ban)
        if symbol not in normalized_pin
    )
    return UserOverrides(pin=normalized_pin, ban=normalized_ban)


def pin_override(current: UserOverrides, symbol: str) -> UserOverrides:
    """Pin one symbol and remove a conflicting ban."""

    normalized = str(symbol or "").strip()
    return normalized_user_overrides(
        pin=(*current.pin, normalized),
        ban=tuple(item for item in current.ban if item != normalized),
    )


def ban_override(current: UserOverrides, symbol: str) -> UserOverrides:
    """Ban one symbol and remove a conflicting pin."""

    normalized = str(symbol or "").strip()
    return normalized_user_overrides(
        pin=tuple(item for item in current.pin if item != normalized),
        ban=(*current.ban, normalized),
    )


def clear_user_override(current: UserOverrides, symbol: str) -> UserOverrides:
    """Remove both pin and ban intent for one symbol."""

    normalized = str(symbol or "").strip()
    return UserOverrides(
        pin=tuple(item for item in current.pin if item != normalized),
        ban=tuple(item for item in current.ban if item != normalized),
    )


def apply_user_overrides(
    symbols: list[str],
    *,
    pin: tuple[str, ...] | list[str] = (),
    ban: tuple[str, ...] | list[str] = (),
    sticky: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Apply pin/ban while keeping sticky symbols protected outside the quota."""

    banned = set(ban) - set(sticky)
    kept = [symbol for symbol in symbols if symbol not in banned]
    for symbol in pin:
        if symbol not in kept:
            kept.append(symbol)
    return kept
