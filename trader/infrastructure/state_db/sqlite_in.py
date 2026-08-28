"""Conservative SQLite ``IN`` chunking for World Pattern read adapters.

Common SQLite builds cap host parameters at 999. Binding an unbounded id list
in one ``IN`` raises an operational error. Callers must chunk below that limit
and never treat the error as a semantic empty result.

Convention: chunk each unbounded id list at ``SQLITE_IN_CHUNK_SIZE`` (500).
When a statement has several unbounded ``IN`` lists, shrink the chunk so the
combined binds stay under the host-parameter limit. Closed enums (horizons)
stay unchunked.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

SQLITE_HOST_PARAMETER_LIMIT = 999
SQLITE_IN_CHUNK_SIZE = 500


def sqlite_placeholders(count: int) -> str:
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("placeholder count must be a non-negative integer")
    return ",".join("?" for _ in range(count))


def ordered_unique_ids(values: Iterable[object]) -> tuple[str, ...]:
    """Dedupe and sort so chunk boundaries and merges are deterministic."""

    return tuple(sorted({str(item) for item in values if str(item).strip()}))


def sqlite_in_chunk_size(*, unbounded_in_count: int = 1, extra_binds: int = 0) -> int:
    if not isinstance(unbounded_in_count, int) or isinstance(unbounded_in_count, bool) or unbounded_in_count < 1:
        raise ValueError("unbounded_in_count must be a positive integer")
    if not isinstance(extra_binds, int) or isinstance(extra_binds, bool) or extra_binds < 0:
        raise ValueError("extra_binds must be a non-negative integer")
    budget = SQLITE_HOST_PARAMETER_LIMIT - extra_binds
    if budget < unbounded_in_count:
        raise ValueError("not enough SQLite host parameters for the IN clauses")
    return min(SQLITE_IN_CHUNK_SIZE, budget // unbounded_in_count)


def sqlite_in_chunks(
    values: Sequence[object] | Iterable[object],
    *,
    size: int | None = None,
    unbounded_in_count: int = 1,
    extra_binds: int = 0,
) -> tuple[tuple[str, ...], ...]:
    chunk_size = (
        sqlite_in_chunk_size(unbounded_in_count=unbounded_in_count, extra_binds=extra_binds) if size is None else size
    )
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
        raise ValueError("chunk size must be a positive integer")
    ordered = ordered_unique_ids(values)
    if not ordered:
        return ()
    return tuple(ordered[offset : offset + chunk_size] for offset in range(0, len(ordered), chunk_size))


__all__ = [
    "SQLITE_HOST_PARAMETER_LIMIT",
    "SQLITE_IN_CHUNK_SIZE",
    "ordered_unique_ids",
    "sqlite_in_chunk_size",
    "sqlite_in_chunks",
    "sqlite_placeholders",
]
