from __future__ import annotations

from trader.infrastructure.state_db.sqlite_in import (
    SQLITE_IN_CHUNK_SIZE,
    ordered_unique_ids,
    sqlite_in_chunk_size,
    sqlite_in_chunks,
    sqlite_placeholders,
)


def test_ordered_unique_ids_dedupe_and_sort() -> None:
    assert ordered_unique_ids(("c", "a", "b", "a", " ", "")) == ("a", "b", "c")


def test_sqlite_in_chunks_are_deterministic_and_bounded() -> None:
    ids = [f"id-{index:04d}" for index in range(1100)]
    shuffled = list(reversed(ids)) + ids[:10]
    chunks = sqlite_in_chunks(shuffled)
    flattened = tuple(item for chunk in chunks for item in chunk)
    assert flattened == tuple(sorted(ids))
    assert all(len(chunk) <= SQLITE_IN_CHUNK_SIZE for chunk in chunks)
    assert max(len(chunk) for chunk in chunks) == SQLITE_IN_CHUNK_SIZE
    assert len(chunks) >= 3


def test_two_unbounded_in_lists_shrink_below_host_parameter_limit() -> None:
    size = sqlite_in_chunk_size(unbounded_in_count=2, extra_binds=1)
    assert size <= SQLITE_IN_CHUNK_SIZE
    assert size * 2 + 1 <= 999
    assert sqlite_placeholders(3) == "?,?,?"
