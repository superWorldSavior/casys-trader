"""Runtime adapter for market-universe rotation ticks."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from trader.runtime.protocols import LoggerLike


LoadRadarParamsFn = Callable[[Path], object]
BuildOverrideFn = Callable[[], object]
BuildMarketContextFn = Callable[[object], object]
RotationTickFn = Callable[..., object]
PositionsFn = Callable[[], dict]
PlansFn = Callable[[], list]
StickyFn = Callable[[], set[str]]


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


def _load_cached_market_context(
    regime_cache_path: Path,
    *,
    build_market_context_from_regime_fn: BuildMarketContextFn,
) -> object | None:
    if not regime_cache_path.exists():
        return None
    try:
        cached_regime = json.loads(regime_cache_path.read_text(encoding="utf-8"))
        return build_market_context_from_regime_fn(cached_regime)
    except Exception:  # noqa: BLE001 - cached regime is advisory only
        return None


def build_positions_fn(state_dir: str | Path) -> PositionsFn:
    """Return a fail-safe reader for open broker positions."""
    state_dir = Path(state_dir)

    def _positions() -> dict:
        try:
            # SimBroker creates broker.json when opened. Avoid creating a phantom
            # json shadow in sqlite mode or with the wrong default cash.
            if not (state_dir / "broker.json").exists():
                return {}
            from trader.execution.broker import SimBroker

            return SimBroker(state_dir / "broker.json").positions()
        except Exception:  # noqa: BLE001 - sticky state is advisory/fail-safe
            return {}

    return _positions


def build_plans_fn(state_dir: str | Path) -> PlansFn:
    """Return a fail-safe reader for open SQLite trade plans."""
    state_dir = Path(state_dir)

    def _plans() -> list:
        try:
            db_path = state_dir / "casys.db"
            if not db_path.exists():
                return []
            from trader.infrastructure.state_db.connection import open_state_db
            from trader.infrastructure.state_db.trade_plan_store import SqliteTradePlanStore

            return SqliteTradePlanStore(open_state_db(db_path)).open_plans()
        except Exception:  # noqa: BLE001 - sticky state is advisory/fail-safe
            return []

    return _plans


def build_sticky_fn(state_dir: str | Path) -> StickyFn:
    """Compose the market rotation sticky collector from runtime state readers."""
    from trader.market.rotation.collectors import sticky_collector

    positions_fn = build_positions_fn(state_dir)
    plans_fn = build_plans_fn(state_dir)

    def _sticky() -> set[str]:
        return sticky_collector(positions_fn=positions_fn, plans_fn=plans_fn)

    return _sticky


def build_llm_override_fn(
    *,
    acpx_bin: str = "acpx",
    spark_model: str | None = None,
    timeout_s: int = 120,
) -> Callable[[Any], dict]:
    """Build the production LLM override callable for market rotation."""
    from trader.agent import llm
    from trader.market.rotation.override import make_llm_override_fn

    kw: dict[str, Any] = {"acpx_bin": acpx_bin}
    if spark_model is not None:
        kw["spark_model"] = spark_model

    router = llm.build_default_router_from_env(**kw)

    def _complete(prompt: str, *, timeout_s: int) -> str:
        result = router.complete(prompt, timeout_s=timeout_s)
        return getattr(result, "text", "")

    return make_llm_override_fn(_complete, timeout_s=timeout_s)


def tick_market_rotation(
    *,
    config_dir: Path,
    state_dir: Path,
    loop_now: datetime,
    logger: LoggerLike | None = None,
    load_radar_params_fn: LoadRadarParamsFn | None = None,
    build_override_fn: BuildOverrideFn | None = None,
    build_market_context_from_regime_fn: BuildMarketContextFn | None = None,
    rotation_tick_fn: RotationTickFn | None = None,
) -> None:
    """Run the best-effort D10 market rotation tick for daemon.main()."""
    log = logger or _default_logger()
    try:
        if load_radar_params_fn is None:
            from trader.market.radar_config import load_radar_params as load_radar_params_fn
        if rotation_tick_fn is None:
            from trader.market.rotation.venues import tick as rotation_tick_fn
        if build_market_context_from_regime_fn is None:
            from trader.market.rotation.wiring import (
                build_market_context_from_regime as build_market_context_from_regime_fn,
            )

        radar_params = load_radar_params_fn(config_dir)
        override_fn = None
        if radar_params.override_enabled:
            if build_override_fn is None:
                build_override_fn = build_llm_override_fn
            override_fn = build_override_fn()

        market_context = _load_cached_market_context(
            state_dir / "last_regime.json",
            build_market_context_from_regime_fn=build_market_context_from_regime_fn,
        )
        rotation_tick_fn(
            config_dir,
            state_dir,
            loop_now.isoformat(),
            override_fn=override_fn,
            sticky_fn=build_sticky_fn(state_dir),
            market_context=market_context,
        )
    except Exception:  # noqa: BLE001 - rotation must never bring down the daemon
        log.exception("rotation tick (D10) échouée")


def run_cli(
    config_dir: str | Path,
    state_dir: str | Path,
    *,
    fetch_fn: Callable[[list[str]], dict[str, list]] | None = None,
    override_fn: Callable[[Any], dict] | None = None,
    sticky_fn: StickyFn | None = None,
    as_of: str | None = None,
) -> dict:
    """Production facade for the EOD market rotation CLI."""
    from trader.market.rotation.wiring import run_cli as rotation_run_cli

    return rotation_run_cli(
        config_dir,
        state_dir,
        fetch_fn=fetch_fn,
        override_fn=override_fn,
        sticky_fn=sticky_fn,
        build_override_fn=build_llm_override_fn,
        build_sticky_fn=build_sticky_fn,
        as_of=as_of,
    )


def load_effective_universe(universe_path, state_dir) -> list:
    """``symbols:`` de universe.yaml avec pin/ban cockpit appliqués à la lecture.

    Adaptateur pour le daemon (qui ne doit pas importer trader.market.rotation
    directement) : le ban prend effet au cycle suivant même si la rotation n'a
    pas réécrit le fichier ; une position ouverte bannie reste gérée. Jamais
    d'exception.
    """
    from trader.market.rotation.user_overrides import effective_universe_symbols

    try:
        sticky = build_sticky_fn(state_dir)()
    except Exception:  # noqa: BLE001 — collector fail-safe
        sticky = set()
    return effective_universe_symbols(universe_path, positions=sticky)
