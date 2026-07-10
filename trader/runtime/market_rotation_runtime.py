"""Runtime adapter for market-universe rotation ticks."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trader.runtime.protocols import LoggerLike


LoadRadarParamsFn = Callable[[Path], object]
BuildOverrideFn = Callable[[], object]
BuildMarketContextFn = Callable[[object], object]
BuildNewsChallengerFn = Callable[..., object]
BuildPreparedUniverseFn = Callable[[Path], object]
BuildCandidateScopeObserverFn = Callable[[Path], object]
BuildUniverseActivationObserverFn = Callable[[Path], object]
BuildRadarScoreAuditObserverFn = Callable[[Path], object]
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


def build_candidate_scope_observer(state_dir: str | Path) -> Callable[[dict], None]:
    """Persist immutable close snapshots without coupling rotation to storage."""

    from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore

    store = CandidateScopeStore(Path(state_dir) / "candidate_scopes")

    def _observe(record: dict) -> dict[str, str]:
        return store.append(record)

    return _observe


def build_universe_activation_observer(state_dir: str | Path) -> Callable[[dict], dict]:
    """Activate only the mandate slice selected by the synchronous venue path."""

    from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore

    store = UniverseMandateStore(Path(state_dir) / "universe_mandates")

    def _observe(record: dict) -> dict:
        payload = store.activate(
            venue=str(record.get("venue") or ""),
            candidate_scope_id=str(record.get("candidate_scope_id") or ""),
            as_of=str(record.get("as_of") or ""),
            selected_symbols=record.get("selected_hotlist") or (),
            fallback_reason=(
                str(record.get("fallback_reason") or "fallback")
                if record.get("fallback_used")
                else None
            ),
        )
        return {
            "mandate_id": payload.get("mandate_id"),
            "candidate_scope_id": payload.get("candidate_scope_id"),
            "status": payload.get("status"),
        }

    return _observe


def build_radar_score_audit_observer(state_dir: str | Path) -> Callable[[dict], dict]:
    """Persist the shadow score audit without affecting rotation selection."""

    from trader.infrastructure.state_db.shadow import write_json_atomic

    target = Path(state_dir) / "radar_score_audit.json"

    def _observe(record: dict) -> dict[str, str]:
        write_json_atomic(target, record)
        return {
            "path": target.name,
            "input_signature": str(record.get("input_signature") or ""),
        }

    return _observe


def build_prepared_universe_fn(state_dir: str | Path) -> Callable[..., dict]:
    """Read an exact prepared agent selection for synchronous pre-open activation."""

    from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
    from trader.infrastructure.state_db.universe_run_store import UniverseRunStore

    state_path = Path(state_dir)
    run_store = UniverseRunStore(state_path / "universe_runs")
    brief_store = NewsMacroBriefStore(state_path / "news_briefs")

    def _prepared(*, venue: str, candidate_scope_id: str, as_of: str, **_kwargs: Any) -> dict:
        if not candidate_scope_id:
            return {"status": "missing", "reason": "candidate_scope_missing"}
        record = run_store.read_prepared(candidate_scope_id)
        if record is not None:
            valid_until = _parse_datetime(record.get("valid_until"))
            activation_at = _parse_datetime(as_of)
            if valid_until is not None and activation_at is not None and activation_at >= valid_until:
                return {"status": "expired", "reason": "prepared_expired"}
            return record

        latest_run = run_store.read_latest(venue)
        if latest_run is not None and latest_run.get("candidate_scope_id") == candidate_scope_id:
            status = str(latest_run.get("status") or "").strip()
            if status and status != "success":
                reason = str(latest_run.get("error_code") or status).strip()
                return {
                    "status": status,
                    "reason": f"agent_{reason}",
                    "agent_run_id": latest_run.get("agent_run_id"),
                    "brief_ref": latest_run.get("brief_ref"),
                }

        brief = brief_store.read_latest(venue, at=as_of)
        if brief is None:
            return {"status": "missing", "reason": "brief_missing"}
        refs = brief.input_refs if isinstance(brief.input_refs, dict) else {}
        if refs.get("candidate_scope_id") != candidate_scope_id:
            return {"status": "missing", "reason": "brief_scope_mismatch"}
        return {"status": "pending", "reason": "prepare_pending"}

    return _prepared


def _parse_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def tick_market_rotation(
    *,
    config_dir: Path,
    state_dir: Path,
    loop_now: datetime,
    logger: LoggerLike | None = None,
    load_radar_params_fn: LoadRadarParamsFn | None = None,
    build_override_fn: BuildOverrideFn | None = None,
    build_market_context_from_regime_fn: BuildMarketContextFn | None = None,
    build_news_challenger_fn: BuildNewsChallengerFn | None = None,
    build_prepared_universe_provider_fn: BuildPreparedUniverseFn | None = None,
    build_candidate_scope_observer_fn: BuildCandidateScopeObserverFn | None = None,
    build_universe_activation_observer_fn: BuildUniverseActivationObserverFn | None = None,
    build_radar_score_audit_observer_fn: BuildRadarScoreAuditObserverFn | None = None,
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
        if build_news_challenger_fn is None:
            from trader.runtime.news_challenger_runtime import (
                build_news_challenger_fn as build_news_challenger_fn,
            )

        radar_params = load_radar_params_fn(config_dir)
        override_fn = None
        prepared_universe_fn = None
        if radar_params.override_enabled:
            if build_override_fn is not None:
                # Explicit injection keeps CLI/tests and emergency compatibility.
                override_fn = build_override_fn()
            else:
                if build_prepared_universe_provider_fn is None:
                    build_prepared_universe_provider_fn = build_prepared_universe_fn
                prepared_universe_fn = build_prepared_universe_provider_fn(state_dir)

        if build_candidate_scope_observer_fn is None:
            build_candidate_scope_observer_fn = build_candidate_scope_observer
        candidate_scope_observer = build_candidate_scope_observer_fn(state_dir)
        if build_universe_activation_observer_fn is None:
            build_universe_activation_observer_fn = build_universe_activation_observer
        universe_activation_observer = build_universe_activation_observer_fn(state_dir)
        if build_radar_score_audit_observer_fn is None:
            build_radar_score_audit_observer_fn = build_radar_score_audit_observer
        radar_score_audit_observer = build_radar_score_audit_observer_fn(state_dir)

        market_context = _load_cached_market_context(
            state_dir / "last_regime.json",
            build_market_context_from_regime_fn=build_market_context_from_regime_fn,
        )
        try:
            news_challenger_fn = build_news_challenger_fn(
                config_dir=config_dir,
                state_dir=state_dir,
                as_of=loop_now,
            )
        except Exception as exc:  # noqa: BLE001 - scout is advisory, rotation is mandatory
            news_challenger_fn = None
            log.warning("news challenger provider indisponible: %s", exc)
        rotation_tick_fn(
            config_dir,
            state_dir,
            loop_now.isoformat(),
            override_fn=override_fn,
            prepared_universe_fn=prepared_universe_fn,
            candidate_scope_observer=candidate_scope_observer,
            universe_activation_observer=universe_activation_observer,
            radar_score_audit_observer=radar_score_audit_observer,
            sticky_fn=build_sticky_fn(state_dir),
            market_context=market_context,
            news_challenger_fn=news_challenger_fn,
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
    from trader.infrastructure.files.universe_config import effective_universe_symbols

    try:
        sticky = build_sticky_fn(state_dir)()
    except Exception:  # noqa: BLE001 — collector fail-safe
        sticky = set()
    return effective_universe_symbols(universe_path, positions=sticky)
