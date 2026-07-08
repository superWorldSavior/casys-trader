"""Pure predicates for persisted indicator watches."""

from __future__ import annotations


def is_armed_plan(watch: dict) -> bool:
    """Prédicat UNIQUE « plan armé » (source de vérité pour daemon/scheduler/tui)."""
    return str(watch.get("on_trigger")) == "EXECUTE_ORDER" and isinstance(watch.get("order"), dict)
