"""Atomic worker-visible context for one daemon cycle."""

from __future__ import annotations

from dataclasses import dataclass, field


class CycleContextUnavailable(RuntimeError):
    """Raised when a worker asks for a cycle context that is no longer current."""


@dataclass(frozen=True)
class OpenPlansSnapshot:
    rows: tuple[dict, ...] = ()
    raw_plans: tuple[object, ...] = ()


@dataclass(frozen=True)
class ExitValidationInputs:
    bars_by_symbol: dict[str, list] = field(default_factory=dict)
    prices_by_symbol: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerCycleContext:
    cycle_id: str
    as_of: str
    open_plans: OpenPlansSnapshot
    exit_validation: ExitValidationInputs


class WorkerCycleContextHandle:
    """Shared atomic reference to the latest cycle context visible to workers."""

    def __init__(self) -> None:
        self._current: WorkerCycleContext | None = None

    def publish(self, context: WorkerCycleContext) -> None:
        self._current = context

    def current(self) -> WorkerCycleContext | None:
        return self._current

    def current_for_cycle(self, cycle_id: str) -> WorkerCycleContext:
        context = self._current
        if context is None or context.cycle_id != cycle_id:
            raise CycleContextUnavailable(cycle_id)
        return context

    def _context(self, cycle_id: str | None) -> WorkerCycleContext | None:
        if cycle_id is not None:
            return self.current_for_cycle(cycle_id)
        return self._current

    def get_open_plan_rows(self, cycle_id: str | None = None) -> list[dict]:
        context = self._context(cycle_id)
        if context is None:
            return []
        return list(context.open_plans.rows)

    def get_raw_open_plans(self, cycle_id: str | None = None) -> list[object]:
        context = self._context(cycle_id)
        if context is None:
            return []
        return list(context.open_plans.raw_plans)

    def get_open_plans_as_of(self, cycle_id: str | None = None) -> str | None:
        context = self._context(cycle_id)
        return None if context is None else context.as_of

    def get_exit_validation_bars(self, symbol: str, cycle_id: str | None = None) -> list | None:
        context = self._context(cycle_id)
        if context is None:
            return None
        return context.exit_validation.bars_by_symbol.get(symbol)

    def get_exit_validation_price(self, symbol: str, cycle_id: str | None = None) -> float | None:
        context = self._context(cycle_id)
        if context is None:
            return None
        return context.exit_validation.prices_by_symbol.get(symbol)


class SnapshotTradePlanStore:
    def __init__(self, plans: list | tuple) -> None:
        self._plans = list(plans)

    def open_plans(self) -> list:
        return list(self._plans)

    def upsert(self, _plan: object) -> None:
        return None
