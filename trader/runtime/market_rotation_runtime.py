"""Runtime adapter for market-universe rotation ticks."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol


class LoggerLike(Protocol):
    def exception(self, *args: object) -> None: ...


LoadRadarParamsFn = Callable[[Path], object]
BuildOverrideFn = Callable[[], object]
BuildMarketContextFn = Callable[[object], object]
RotationTickFn = Callable[..., object]


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
                from trader.market.rotation.wiring import build_llm_override_fn as build_override_fn
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
            market_context=market_context,
        )
    except Exception:  # noqa: BLE001 - rotation must never bring down the daemon
        log.exception("rotation tick (D10) échouée")


def load_effective_universe(universe_path, state_dir) -> list:
    """``symbols:`` de universe.yaml avec pin/ban cockpit appliqués à la lecture.

    Adaptateur pour le daemon (qui ne doit pas importer trader.market.rotation
    directement) : le ban prend effet au cycle suivant même si la rotation n'a
    pas réécrit le fichier ; une position ouverte bannie reste gérée. Jamais
    d'exception.
    """
    from trader.market.rotation.collectors import build_positions_fn
    from trader.market.rotation.user_overrides import effective_universe_symbols

    try:
        positions = set(build_positions_fn(state_dir)().keys())
    except Exception:  # noqa: BLE001 — collector fail-safe
        positions = set()
    return effective_universe_symbols(universe_path, positions=positions)
