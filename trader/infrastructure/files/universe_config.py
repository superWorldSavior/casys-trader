"""Atomic YAML adapter for the active universe and operator pin/ban state."""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import yaml

from trader.domain.universe.user_overrides import (
    UserOverrides,
    apply_user_overrides,
    ban_override,
    clear_user_override,
    normalize_override_symbols,
    pin_override,
)


class UniverseWriteError(ValueError):
    """Raised when an empty active universe would be persisted."""


@contextlib.contextmanager
def universe_write_lock(universe_path: str | Path) -> Iterator[None]:
    """Serialize read-modify-write operations across local processes."""

    lock_path = Path(f"{universe_path}.lock")
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_user_overrides(universe_path: str | Path) -> UserOverrides:
    """Read operator overrides fail-safe from ``universe.yaml``."""

    try:
        data = yaml.safe_load(Path(universe_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return UserOverrides()
    raw = data.get("overrides") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return UserOverrides()
    pin = normalize_override_symbols(raw.get("pin"))
    ban = tuple(
        symbol
        for symbol in normalize_override_symbols(raw.get("ban"))
        if symbol not in pin
    )
    return UserOverrides(pin=pin, ban=ban)


def overrides_block(overrides: UserOverrides) -> dict[str, list[str]] | None:
    """Serialize non-empty override intent for the YAML adapter."""

    block: dict[str, list[str]] = {}
    if overrides.pin:
        block["pin"] = list(overrides.pin)
    if overrides.ban:
        block["ban"] = list(overrides.ban)
    return block or None


def save_user_overrides(
    universe_path: str | Path,
    overrides: UserOverrides,
) -> None:
    """Atomically persist overrides while preserving every unrelated key."""

    path = Path(universe_path)
    with universe_write_lock(path):
        _write_overrides_locked(path, overrides)


def _write_overrides_locked(path: Path, overrides: UserOverrides) -> None:
    try:
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}

    data: dict = {"symbols": existing.get("symbols") or []}
    for key, value in existing.items():
        if key not in ("symbols", "overrides"):
            data[key] = value
    block = overrides_block(overrides)
    if block is not None:
        data["overrides"] = block

    fd, tmp_path = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
            yaml.safe_dump(data, file_handle, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _mutate_overrides(
    universe_path: str | Path,
    mutate: Callable[[UserOverrides], UserOverrides],
) -> UserOverrides:
    path = Path(universe_path)
    with universe_write_lock(path):
        updated = mutate(load_user_overrides(path))
        _write_overrides_locked(path, updated)
    return updated


def pin_symbol(universe_path: str | Path, symbol: str) -> UserOverrides:
    return _mutate_overrides(
        universe_path,
        lambda current: pin_override(current, symbol),
    )


def ban_symbol(universe_path: str | Path, symbol: str) -> UserOverrides:
    return _mutate_overrides(
        universe_path,
        lambda current: ban_override(current, symbol),
    )


def clear_override(universe_path: str | Path, symbol: str) -> UserOverrides:
    return _mutate_overrides(
        universe_path,
        lambda current: clear_user_override(current, symbol),
    )


def write_universe_atomic(path: str | Path, symbols: list[str]) -> None:
    """Atomically replace active symbols while preserving operator keys."""

    if not symbols:
        raise UniverseWriteError("symbols ne peut pas être vide")
    universe_path = Path(path)
    with universe_write_lock(universe_path):
        data: dict = {"symbols": symbols}
        try:
            existing = yaml.safe_load(universe_path.read_text(encoding="utf-8")) or {}
            if isinstance(existing, dict):
                for key, value in existing.items():
                    if key != "symbols":
                        data[key] = value
        except Exception:
            pass

        fd, tmp_path = tempfile.mkstemp(dir=universe_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
                yaml.safe_dump(data, file_handle, sort_keys=False)
            os.replace(tmp_path, universe_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def write_universe_if_changed(path: str | Path, symbols: list[str]) -> bool:
    """Persist a non-empty universe only when its symbol set changes."""

    if not symbols:
        return False
    universe_path = Path(path)
    try:
        data = yaml.safe_load(universe_path.read_text(encoding="utf-8"))
        current_symbols = data.get("symbols") or [] if isinstance(data, dict) else []
    except Exception:
        current_symbols = []
    if set(symbols) == set(current_symbols):
        return False
    write_universe_atomic(universe_path, symbols)
    return True


def effective_universe_symbols(
    universe_path: str | Path,
    *,
    positions: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Read active symbols with current pin/ban policy applied fail-safe."""

    try:
        data = yaml.safe_load(Path(universe_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    symbols = (
        [str(symbol) for symbol in (data.get("symbols") or []) if symbol]
        if isinstance(data, dict)
        else []
    )
    overrides = load_user_overrides(universe_path)
    return apply_user_overrides(
        symbols,
        pin=overrides.pin,
        ban=overrides.ban,
        sticky=positions,
    )
