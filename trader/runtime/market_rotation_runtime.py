"""Runtime adapter for market-universe rotation ticks."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
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
WatchesFn = Callable[[], list]
PendingReplaySymbolsFn = Callable[[], set[str]]
PendingTriggerSymbolsFn = Callable[[], set[str]]
StickyFn = Callable[[], set[str]]

STALE_REPLAY_STICKY_MAX_HOURS = 36.0


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


def venue_for_symbol(symbol: str) -> str:
    """Expose venue classification without leaking rotation internals to daemon."""

    from trader.market.rotation.wiring import venue_of

    return venue_of(symbol)


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
            db_path = state_dir / "casys.db"
            if db_path.exists():
                from trader.infrastructure.state_db.broker_store import SqliteBroker
                from trader.infrastructure.state_db.connection import open_state_db

                return SqliteBroker(open_state_db(db_path)).positions()

            # SimBroker creates broker.json when opened. Avoid creating a phantom
            # legacy file when no persisted broker state exists.
            broker_json_path = state_dir / "broker.json"
            if not broker_json_path.exists():
                return {}
            from trader.infrastructure.state_db.sim_broker import SimBroker

            return SimBroker(broker_json_path).positions()
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


def build_watches_fn(
    state_dir: str | Path,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> WatchesFn:
    """Return a non-destructive snapshot of active indicator watches.

    ``active_indicator_watches`` is deliberately avoided because both scheduler
    backends purge expired records as a side effect. Rotation only needs a
    read-only lease snapshot; lifecycle expiry remains owned by the cycle.
    """
    state_dir = Path(state_dir)
    clock = now_fn or (lambda: datetime.now(timezone.utc))

    def _watches() -> list:
        try:
            db_path = state_dir / "casys.db"
            if db_path.exists():
                from trader.infrastructure.state_db.connection import open_state_db
                from trader.infrastructure.state_db.scheduler_store import SqliteScheduler

                raw_watches = SqliteScheduler(open_state_db(db_path)).watches().values()
            else:
                scheduler_path = state_dir / "scheduler.json"
                if not scheduler_path.exists():
                    return []
                raw = json.loads(scheduler_path.read_text(encoding="utf-8"))
                watches = raw.get("indicator_watches", {})
                if not isinstance(watches, dict):
                    return []
                raw_watches = watches.values()

            now = clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            else:
                now = now.astimezone(timezone.utc)
            active: list[dict] = []
            for watch in raw_watches:
                if not isinstance(watch, dict):
                    continue
                expires_at = _parse_datetime(watch.get("expires_at"))
                if expires_at is not None and expires_at <= now:
                    continue
                active.append(dict(watch))
            return active
        except Exception:  # noqa: BLE001 - sticky state is advisory/fail-safe
            return []

    return _watches


def build_pending_replay_symbols_fn(
    state_dir: str | Path,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> PendingReplaySymbolsFn:
    """Return recent stale→fresh leases without retaining orphaned symbols forever."""
    state_dir = Path(state_dir)
    clock = now_fn or (lambda: datetime.now(timezone.utc))

    def _pending_symbols() -> set[str]:
        try:
            db_path = state_dir / "casys.db"
            if not db_path.exists():
                return set()
            from trader.domain.planning import relevance_gate
            from trader.infrastructure.state_db.connection import open_state_db
            from trader.infrastructure.state_db.llm_gate_store import LlmGateStore
            from trader.infrastructure.state_db.scheduler_store import SqliteScheduler

            db = open_state_db(db_path)
            snapshot = LlmGateStore(db).load_state()
            scheduler = SqliteScheduler(db)
            now = clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            else:
                now = now.astimezone(timezone.utc)
            cutoff = now - timedelta(hours=STALE_REPLAY_STICKY_MAX_HOURS)
            expected_state_dir = state_dir.resolve()
            pending: set[str] = set()
            for key, fingerprints in snapshot.last_wake_fingerprints.items():
                persisted_state_dir, symbol = key
                try:
                    same_state_dir = Path(persisted_state_dir).resolve() == expected_state_dir
                except (OSError, RuntimeError, TypeError, ValueError):
                    same_state_dir = persisted_state_dir == str(state_dir)
                if not same_state_dir or not relevance_gate.is_pending_stale_review(
                    fingerprints
                ):
                    continue
                last_review_at = snapshot.last_llm_at.get(key)
                scheduled_wake = (
                    scheduler.next_wake(symbol)
                    if scheduler.has_symbol_wake(symbol)
                    else None
                )
                probe_marker = _parse_datetime(
                    fingerprints.get(
                        relevance_gate.FRESH_PROBE_WAKE_FINGERPRINT_KEY
                    )
                )
                owned_probe_deadline = None
                if probe_marker is not None:
                    owned_probe_deadline = probe_marker + timedelta(
                        hours=STALE_REPLAY_STICKY_MAX_HOURS
                    )
                    try:
                        from trader.domain.market import sessions as market_sessions

                        next_open_after_marker = market_sessions.next_regular_session_open(
                            probe_marker,
                            symbol=symbol,
                        )
                        owned_probe_deadline = max(
                            owned_probe_deadline,
                            next_open_after_marker
                            + timedelta(hours=STALE_REPLAY_STICKY_MAX_HOURS),
                        )
                    except Exception:  # noqa: BLE001 - bounded fallback above
                        pass
                owned_probe_wake = (
                    probe_marker is not None
                    and owned_probe_deadline is not None
                    and scheduled_wake == probe_marker
                    and now <= owned_probe_deadline
                )
                legacy_wake_lease = (
                    probe_marker is None
                    and scheduled_wake is not None
                    and now - timedelta(hours=STALE_REPLAY_STICKY_MAX_HOURS)
                    <= scheduled_wake
                    <= now + timedelta(days=7)
                )
                if owned_probe_wake or legacy_wake_lease or (
                    last_review_at is not None and last_review_at >= cutoff
                ):
                    pending.add(symbol)
            return pending
        except Exception:  # noqa: BLE001 - sticky state is advisory/fail-safe
            return set()

    return _pending_symbols


def build_pending_trigger_symbols_fn(
    state_dir: str | Path,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> PendingTriggerSymbolsFn:
    """Return symbols protected by an unacknowledged trigger delivery."""
    state_dir = Path(state_dir)
    clock = now_fn or (lambda: datetime.now(timezone.utc))

    def _pending_symbols() -> set[str]:
        try:
            db_path = state_dir / "casys.db"
            if db_path.exists():
                from trader.infrastructure.state_db.connection import open_state_db
                from trader.infrastructure.state_db.scheduler_store import SqliteScheduler

                scheduler = SqliteScheduler(open_state_db(db_path))
            else:
                scheduler_path = state_dir / "scheduler.json"
                if not scheduler_path.exists():
                    return set()
                from trader.infrastructure.state_db.scheduler_json import Scheduler

                scheduler = Scheduler(scheduler_path)  # type: ignore[assignment]
            return scheduler.pending_indicator_trigger_symbols(now=clock())
        except Exception:  # noqa: BLE001 - sticky state is advisory/fail-safe
            return set()

    return _pending_symbols


def build_sticky_fn(
    state_dir: str | Path,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> StickyFn:
    """Compose the market rotation sticky collector from runtime state readers."""
    from trader.market.rotation.collectors import sticky_collector

    positions_fn = build_positions_fn(state_dir)
    plans_fn = build_plans_fn(state_dir)
    watches_fn = build_watches_fn(state_dir, now_fn=now_fn)
    pending_replay_symbols_fn = build_pending_replay_symbols_fn(
        state_dir,
        now_fn=now_fn,
    )
    pending_trigger_symbols_fn = build_pending_trigger_symbols_fn(
        state_dir,
        now_fn=now_fn,
    )

    def _pending_symbols() -> set[str]:
        return pending_replay_symbols_fn() | pending_trigger_symbols_fn()

    def _sticky() -> set[str]:
        return sticky_collector(
            positions_fn=positions_fn,
            plans_fn=plans_fn,
            watches_fn=watches_fn,
            pending_symbols_fn=_pending_symbols,
        )

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

    kw: dict[str, Any] = {
        "acpx_bin": acpx_bin,
        # Rotation/universe is an analyst role, not the per-symbol trader.
        "spark_model": spark_model or llm.DEFAULT_ANALYST_MODEL,
    }

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
        from trader.runtime.world_model_runtime import build_universe_written_scope_observer

        universe_written_observer = build_universe_written_scope_observer(config_dir)

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
            sticky_fn=build_sticky_fn(
                state_dir,
                now_fn=lambda: loop_now,
            ),
            market_context=market_context,
            news_challenger_fn=news_challenger_fn,
            universe_written_observer=universe_written_observer,
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

    from trader.runtime.world_model_runtime import build_universe_written_scope_observer

    return rotation_run_cli(
        config_dir,
        state_dir,
        fetch_fn=fetch_fn,
        override_fn=override_fn,
        sticky_fn=sticky_fn,
        build_override_fn=build_llm_override_fn,
        build_sticky_fn=build_sticky_fn,
        as_of=as_of,
        universe_written_observer=build_universe_written_scope_observer(config_dir),
    )


def load_effective_universe(
    universe_path,
    state_dir,
    *,
    now: datetime | None = None,
) -> list:
    """``symbols:`` de universe.yaml avec pin/ban cockpit appliqués à la lecture.

    Adaptateur pour le daemon (qui ne doit pas importer trader.market.rotation
    directement) : le ban prend effet au cycle suivant même si la rotation n'a
    pas réécrit le fichier ; une position ouverte bannie reste gérée. Jamais
    d'exception.
    """
    from trader.infrastructure.files.universe_config import effective_universe_symbols

    try:
        sticky = build_sticky_fn(
            state_dir,
            now_fn=(None if now is None else lambda: now),
        )()
    except Exception:  # noqa: BLE001 — collector fail-safe
        sticky = set()
    return effective_universe_symbols(universe_path, positions=sticky)
