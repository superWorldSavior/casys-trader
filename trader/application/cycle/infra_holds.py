"""Application helpers for infrastructure-authored HOLD decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from trader.application.cycle.schedule import stale_backoff_wake_minutes
from trader.domain.planning import relevance_gate


class SymbolWakeSource(Protocol):
    def symbols_with_wake(self) -> set[str]: ...


class SessionWakeClamp(Protocol):
    def __call__(self, wake_minutes: float, *, now: datetime, symbol: str) -> float: ...


def _wake_matches_fingerprint(
    wake_source: SymbolWakeSource | None,
    *,
    symbol: str,
    fingerprints: Mapping[str, str] | None,
    fingerprint_key: str,
) -> bool:
    """Return whether the current override is the persisted internal wake."""
    marker = (fingerprints or {}).get(fingerprint_key)
    next_wake = getattr(wake_source, "next_wake", None)
    if not marker or not callable(next_wake):
        return False
    try:
        expected = datetime.fromisoformat(str(marker).replace("Z", "+00:00"))
        actual = next_wake(symbol)
    except (TypeError, ValueError):
        return False
    if actual is None:
        return False
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    return actual.astimezone(timezone.utc) == expected.astimezone(timezone.utc)


def _is_internal_fresh_probe(
    wake_source: SymbolWakeSource | None,
    *,
    symbol: str,
    fingerprints: Mapping[str, str] | None,
) -> bool:
    """Identify the scheduler override owned by the stale→fresh probe.

    Ordinary agent wakes must keep bypassing the gate.  The exact timestamp is
    persisted with the LLM-gate context so a restart can distinguish the
    internal probe from an agent-requested wake without a second state store.
    """
    return _wake_matches_fingerprint(
        wake_source,
        symbol=symbol,
        fingerprints=fingerprints,
        fingerprint_key=relevance_gate.FRESH_PROBE_WAKE_FINGERPRINT_KEY,
    )


def _is_internal_hot_review_wake(
    wake_source: SymbolWakeSource | None,
    *,
    symbol: str,
    fingerprints: Mapping[str, str] | None,
) -> bool:
    """Identify the scheduler override owned by the 1-hour hot cadence."""
    return _wake_matches_fingerprint(
        wake_source,
        symbol=symbol,
        fingerprints=fingerprints,
        fingerprint_key=relevance_gate.HOT_REVIEW_WAKE_FINGERPRINT_KEY,
    )


def _due_symbol_wakes(
    wake_source: SymbolWakeSource | None,
    *,
    symbols: set[str],
    now: datetime,
) -> set[str]:
    """Filter overrides to wakes that are actually due in this cycle."""
    if wake_source is None:
        return set()
    next_wake = getattr(wake_source, "next_wake", None)
    if not callable(next_wake):
        return symbols
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    due: set[str] = set()
    for symbol in symbols:
        try:
            wake_at = next_wake(symbol)
        except (TypeError, ValueError):
            continue
        if wake_at is None:
            continue
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=timezone.utc)
        if wake_at.astimezone(timezone.utc) <= now_utc.astimezone(timezone.utc):
            due.add(symbol)
    return due


def _current_trigger_fingerprint(
    triggers: Sequence[Mapping[str, Any]],
    *,
    runtime_bar_ts: str | None,
) -> str | None:
    """Identify the exact trigger claim, not merely its shared 15-minute bar."""
    tokens: list[str] = []
    for index, trigger in enumerate(triggers):
        trigger_id = str(
            trigger.get("watch_id")
            or trigger.get("plan_id")
            or f"{trigger.get('source') or 'trigger'}:{index}"
        )
        closed_bar_key = str(
            trigger.get("closed_bar_key") or runtime_bar_ts or "unknown_bar"
        )
        tokens.append(f"{trigger_id}@{closed_bar_key}")
    return relevance_gate.reviewed_trigger_fingerprint(tokens)


@dataclass(frozen=True)
class QuietGateResult:
    kept_symbols: list[str]
    gated_symbols: list[str]
    entries: list[dict]
    reasons: dict[str, str]
    persistent_reasons: dict[str, tuple[str, ...]] = field(default_factory=dict)
    persistent_fingerprints: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class InfraHoldEvent:
    name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class StaleMarketHoldResult:
    new_streak: int
    wake_minutes: float
    entry: dict | None
    event: InfraHoldEvent | None


def quiet_gate_decisions(
    *,
    symbols: list[str],
    now: datetime,
    state_key: str,
    last_llm_at: Mapping[tuple[str, str], datetime],
    cockpit: dict,
    regime_families: Mapping[str, dict],
    active_families: Mapping[str, list[str]],
    wake_source: SymbolWakeSource | None,
    triggers_by_symbol: Mapping[str, list],
    held_symbols: set[str],
    runtime_data_source_by_sym: Mapping[str, object],
    last_wake_reasons: Mapping[tuple[str, str], Sequence[str]] | None = None,
    last_wake_fingerprints: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
    execution_eligibility: Mapping[str, Mapping[str, Any]] | None = None,
    hot_setup_symbols: set[str] | frozenset[str] | None = None,
    runtime_bar_ts_by_symbol: Mapping[str, str] | None = None,
) -> QuietGateResult:
    """Split decidable symbols into LLM-needed and quiet infra-HOLD entries."""
    activity = relevance_gate.cockpit_activity(cockpit)
    strong_families = {family for family, bias in regime_families.items() if (bias.get("frac") or 0.0) >= 0.70}
    family_of = {member: family for family, members in active_families.items() for member in members}
    configured_wakes = (
        wake_source.symbols_with_wake() if wake_source is not None else set()
    )
    agent_wakes = _due_symbol_wakes(
        wake_source,
        symbols=configured_wakes,
        now=now,
    )
    wake_reasons = last_wake_reasons or {}
    wake_fingerprints = last_wake_fingerprints or {}
    eligibility = execution_eligibility or {}
    hot_setups = hot_setup_symbols or set()
    runtime_bars = runtime_bar_ts_by_symbol or {}

    gated_symbols: list[str] = []
    kept_symbols: list[str] = []
    entries: list[dict] = []
    reasons: dict[str, str] = {}
    persistent_reasons: dict[str, tuple[str, ...]] = {}
    persistent_fingerprints: dict[str, dict[str, str]] = {}
    for symbol in symbols:
        key = (state_key, symbol)
        last_seen = last_llm_at.get(key)
        previous_fingerprints = wake_fingerprints.get(key)
        hours = None if last_seen is None else (now - last_seen).total_seconds() / 3600.0
        activity_for_symbol = activity.get(symbol) or {}
        family = family_of.get(symbol)
        family_regime_strong = family in strong_families
        family_bias = regime_families.get(family) if family is not None else None
        regime_direction = (
            str(family_bias.get("dir") or "").strip().lower()
            if isinstance(family_bias, Mapping)
            else ""
        )
        family_regime_fingerprint = (
            f"regime:{family}:{regime_direction}"
            if family_regime_strong and regime_direction in {"up", "down"}
            else None
        )
        current_fingerprints = relevance_gate.persistent_wake_fingerprints(
            family_regime_fingerprint=family_regime_fingerprint,
            stretched=activity_for_symbol.get("stretched"),
            aligned=activity_for_symbol.get("aligned"),
            sig=activity_for_symbol.get("sig"),
        )
        execution = (eligibility.get(symbol) or {}).get("execution") or {}
        bar_ts = runtime_bars.get(symbol) or execution.get("last_runtime_bar_ts")
        bar_ts_text = str(bar_ts) if bar_ts else None
        symbol_triggers = triggers_by_symbol.get(symbol) or []
        trigger_fingerprint = _current_trigger_fingerprint(
            symbol_triggers,
            runtime_bar_ts=bar_ts_text,
        )
        needed, gate_reason = relevance_gate.symbol_needs_llm(
            agent_requested_wake=(
                symbol in agent_wakes
                and not _is_internal_fresh_probe(
                    wake_source,
                    symbol=symbol,
                    fingerprints=previous_fingerprints,
                )
                and not _is_internal_hot_review_wake(
                    wake_source,
                    symbol=symbol,
                    fingerprints=previous_fingerprints,
                )
            ),
            has_trigger=bool(symbol_triggers),
            has_position=symbol in held_symbols,
            family_regime_strong=family_regime_strong,
            stretched=activity_for_symbol.get("stretched"),
            aligned=activity_for_symbol.get("aligned"),
            sig=activity_for_symbol.get("sig"),
            hours_since_last_llm=hours,
            last_wake_reasons=wake_reasons.get(key),
            family_regime_fingerprint=family_regime_fingerprint,
            last_wake_fingerprints=previous_fingerprints,
            session_open=relevance_gate.session_is_open(execution),
            execution_enabled=relevance_gate.execution_is_enabled(execution),
            has_hot_setup=symbol in hot_setups,
            current_runtime_bar_ts=bar_ts_text,
            current_trigger_fingerprint=trigger_fingerprint,
        )
        if needed:
            kept_symbols.append(symbol)
            reasons[symbol] = gate_reason
            persistent_reasons[symbol] = relevance_gate.persistent_wake_reasons(
                family_regime_strong=family_regime_strong,
                stretched=activity_for_symbol.get("stretched"),
                aligned=activity_for_symbol.get("aligned"),
                sig=activity_for_symbol.get("sig"),
            )
            fingerprints = dict(current_fingerprints)
            if trigger_fingerprint is not None:
                fingerprints[
                    relevance_gate.REVIEWED_TRIGGER_FINGERPRINT_KEY
                ] = trigger_fingerprint
            if relevance_gate.should_mark_stale_review_pending(
                execution
            ) or (
                relevance_gate.is_pending_stale_review(previous_fingerprints)
                and not relevance_gate.execution_is_enabled(execution)
            ):
                fingerprints[relevance_gate.STALE_REVIEW_FINGERPRINT_KEY] = (
                    relevance_gate.STALE_REVIEW_PENDING
                )
            persistent_fingerprints[symbol] = fingerprints
            continue

        gated_symbols.append(symbol)
        entries.append(
            {
                "symbol": symbol,
                "action": "HOLD",
                "qty": 0.0,
                "confidence": 0.0,
                "rationale": "quiet_gate",
                "next_wake_in_minutes": None,
                "intent": "HOLD",
                "trade_plan_created": False,
                "executed": False,
                "reason": "quiet_gate",
                **(
                    {"relevance_gate_reason": gate_reason}
                    if gate_reason != "quiet"
                    else {}
                ),
                "decision_reason_code": "NO_EDGE",
                "decision_source": "infra",
                "model_called": False,
                "data_source": runtime_data_source_by_sym.get(symbol),
            }
        )

    return QuietGateResult(
        kept_symbols=kept_symbols,
        gated_symbols=gated_symbols,
        entries=entries,
        reasons=reasons,
        persistent_reasons=persistent_reasons,
        persistent_fingerprints=persistent_fingerprints,
    )


def stale_market_hold_decision(
    *,
    symbol: str,
    stale_data: Mapping[str, Any],
    current_streak: int,
    default_wake_minutes: float,
    now: datetime,
    clamp_wake_to_session_open: SessionWakeClamp,
    stale_armed_plan: Mapping[str, Any] | None,
    runtime_data_source: object,
) -> StaleMarketHoldResult:
    """Build the infra HOLD or lightweight backoff event for non-decidable stale data."""
    wake_minutes = stale_backoff_wake_minutes(
        current_streak,
        default_wake_minutes=default_wake_minutes,
    )
    wake_minutes = clamp_wake_to_session_open(wake_minutes, now=now, symbol=symbol)
    new_streak = current_streak + 1

    should_record_stale = current_streak == 0 or stale_armed_plan is not None
    if should_record_stale:
        entry = {
            "symbol": symbol,
            **(
                {
                    "armed_plan_id": stale_armed_plan["id"],
                    "armed_plan_order": stale_armed_plan["order"],
                }
                if stale_armed_plan is not None
                else {}
            ),
            "action": "HOLD",
            "qty": 0.0,
            "confidence": 0.0,
            "rationale": "stale_market_data",
            "next_wake_in_minutes": wake_minutes,
            "intent": "HOLD",
            "trade_plan_created": False,
            "executed": False,
            "reason": "stale_market_data",
            "decision_reason_code": "DATA_STALE",
            "decision_source": "infra",
            "model_called": False,
            "stale_streak": new_streak,
            "data_source": runtime_data_source,
            **dict(stale_data),
        }
        return StaleMarketHoldResult(
            new_streak=new_streak,
            wake_minutes=wake_minutes,
            entry=entry,
            event=None,
        )

    return StaleMarketHoldResult(
        new_streak=new_streak,
        wake_minutes=wake_minutes,
        entry=None,
        event=InfraHoldEvent(
            "stale_backoff",
            {
                "symbol": symbol,
                "streak": new_streak,
                "next_wake_minutes": wake_minutes,
                "stale_reason": stale_data.get("stale_reason"),
                "data_age_minutes": stale_data.get("data_age_minutes"),
            },
        ),
    )
