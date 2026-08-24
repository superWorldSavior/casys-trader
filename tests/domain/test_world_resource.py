from __future__ import annotations

from trader.domain.world_resource import (
    DEFAULT_MAX_DB_BYTES,
    DEFAULT_MIN_FREE_BYTES,
    DEFAULT_WARN_INTERVAL_SECONDS,
    REASON_DB_SIZE_EXCEEDED,
    REASON_FREE_SPACE_BELOW_RESERVE,
    REASON_WITHIN_BUDGET,
    WORLD_RESOURCE_BUDGET_SCHEMA,
    WorldResourceBudget,
    WorldResourceUsage,
    decide_world_resource_budget,
)

GiB = 1024 ** 3


def _budget(**overrides: object) -> WorldResourceBudget:
    values: dict[str, object] = {
        "schema_version": WORLD_RESOURCE_BUDGET_SCHEMA,
        "max_db_bytes": 2 * GiB,
        "min_free_bytes": 3 * GiB,
        "warn_interval_seconds": 300,
    }
    values.update(overrides)
    return WorldResourceBudget(**values)  # type: ignore[arg-type]


def _usage(**overrides: object) -> WorldResourceUsage:
    values: dict[str, object] = {
        "logical_bytes": 64 * 1024 * 1024,
        "on_disk_bytes": 80 * 1024 * 1024,
        "free_bytes": 7 * GiB,
        "store_exists": True,
    }
    values.update(overrides)
    return WorldResourceUsage(**values)  # type: ignore[arg-type]


def test_conservative_defaults_fit_a_one_week_pilot_on_a_7gib_host() -> None:
    budget = WorldResourceBudget.conservative_defaults()
    assert budget.schema_version == WORLD_RESOURCE_BUDGET_SCHEMA
    assert budget.max_db_bytes == DEFAULT_MAX_DB_BYTES == 2 * GiB
    assert budget.min_free_bytes == DEFAULT_MIN_FREE_BYTES == 3 * GiB
    assert budget.warn_interval_seconds == DEFAULT_WARN_INTERVAL_SECONDS == 300
    assert budget.max_db_bytes < 7 * GiB
    assert budget.min_free_bytes < 7 * GiB
    assert budget.max_db_bytes + budget.min_free_bytes <= 7 * GiB


def test_under_budget_allows_the_write_batch() -> None:
    decision = decide_world_resource_budget(_budget(), _usage())
    assert decision.allowed is True
    assert decision.status == "allowed"
    assert decision.reason == REASON_WITHIN_BUDGET
    assert decision.breaches == ()
    assert decision.usage is not None
    assert decision.usage.store_exists is True


def test_logical_db_size_at_cap_skips_writes() -> None:
    decision = decide_world_resource_budget(
        _budget(),
        _usage(logical_bytes=2 * GiB, on_disk_bytes=100),
    )
    assert decision.allowed is False
    assert decision.status == "skipped"
    assert decision.reason == REASON_DB_SIZE_EXCEEDED
    assert decision.breaches == (REASON_DB_SIZE_EXCEEDED,)


def test_on_disk_db_size_over_cap_skips_writes() -> None:
    decision = decide_world_resource_budget(
        _budget(),
        _usage(logical_bytes=100, on_disk_bytes=2 * GiB + 1),
    )
    assert decision.allowed is False
    assert decision.reason == REASON_DB_SIZE_EXCEEDED
    assert decision.breaches == (REASON_DB_SIZE_EXCEEDED,)


def test_free_space_below_reserve_skips_writes() -> None:
    decision = decide_world_resource_budget(
        _budget(),
        _usage(free_bytes=3 * GiB - 1),
    )
    assert decision.allowed is False
    assert decision.reason == REASON_FREE_SPACE_BELOW_RESERVE
    assert decision.breaches == (REASON_FREE_SPACE_BELOW_RESERVE,)


def test_free_space_equal_to_reserve_is_still_allowed() -> None:
    decision = decide_world_resource_budget(
        _budget(),
        _usage(free_bytes=3 * GiB),
    )
    assert decision.allowed is True
    assert decision.reason == REASON_WITHIN_BUDGET


def test_missing_store_is_under_budget_when_free_space_holds() -> None:
    decision = decide_world_resource_budget(
        _budget(),
        _usage(logical_bytes=0, on_disk_bytes=0, store_exists=False, free_bytes=7 * GiB),
    )
    assert decision.allowed is True
    assert decision.reason == REASON_WITHIN_BUDGET
    assert decision.usage is not None
    assert decision.usage.store_exists is False


def test_db_and_free_space_breaches_are_both_recorded() -> None:
    decision = decide_world_resource_budget(
        _budget(),
        _usage(logical_bytes=2 * GiB, on_disk_bytes=2 * GiB, free_bytes=0),
    )
    assert decision.allowed is False
    assert decision.reason == REASON_DB_SIZE_EXCEEDED
    assert decision.breaches == (REASON_DB_SIZE_EXCEEDED, REASON_FREE_SPACE_BELOW_RESERVE)
