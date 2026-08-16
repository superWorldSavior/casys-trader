from __future__ import annotations

from trader.runtime.cycle_timings import (
    STAGE_KEYS,
    StageClock,
    attach_stage_timings,
    mark_end,
    mark_start,
    try_stage_clock,
)


class _FakeMono:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


def test_stage_clock_records_integer_milliseconds() -> None:
    mono = _FakeMono()
    clock = StageClock(monotonic=mono)
    clock.mark_start("snapshot_ms")
    mono.now = 10.1234
    clock.mark_end("snapshot_ms")
    mono.now = 10.2500
    timings = clock.as_ms()
    assert timings["snapshot_ms"] == 123
    assert timings["total_ms"] == 250
    assert all(isinstance(value, int) for value in timings.values())


def test_stage_clock_swallows_monotonic_errors() -> None:
    calls = {"n": 0}

    def boom() -> float:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("clock broken")
        return 1.0

    clock = StageClock(monotonic=boom)
    clock.mark_start("decide_ms")
    clock.mark_end("decide_ms")
    assert clock.as_ms() == {}


def test_mark_helpers_accept_none_and_broken_clock() -> None:
    mark_start(None, "snapshot_ms")
    mark_end(None, "snapshot_ms")

    class Broken:
        def mark_start(self, name: str) -> None:
            raise RuntimeError("nope")

        def mark_end(self, name: str) -> None:
            raise RuntimeError("nope")

    broken = Broken()
    mark_start(broken, "snapshot_ms")  # type: ignore[arg-type]
    mark_end(broken, "snapshot_ms")  # type: ignore[arg-type]


def test_attach_stage_timings_is_additive_and_safe() -> None:
    clock = StageClock(monotonic=_FakeMono())
    clock.mark_start("record_ms")
    clock.mark_end("record_ms")
    payload = attach_stage_timings({"decisions_done": 2}, clock)
    assert payload["decisions_done"] == 2
    assert set(payload["stage_timings_ms"]) >= {"record_ms", "total_ms"}
    assert attach_stage_timings({"decisions_done": 1}, None) == {"decisions_done": 1}


def test_attach_stage_timings_swallows_as_ms_errors() -> None:
    class Boom(StageClock):
        def as_ms(self) -> dict[str, int]:
            raise RuntimeError("as_ms failed")

    payload = attach_stage_timings({"model_calls_used": 3}, Boom())
    assert payload == {"model_calls_used": 3}


def test_try_stage_clock_returns_usable_clock() -> None:
    clock = try_stage_clock()
    assert clock is not None
    assert set(STAGE_KEYS) == {
        "snapshot_ms",
        "gate_scope_ms",
        "decide_ms",
        "risk_execute_ms",
        "record_ms",
    }


def test_missing_end_does_not_emit_stage() -> None:
    clock = StageClock()
    clock.mark_start("decide_ms")
    assert "decide_ms" not in clock.as_ms()
