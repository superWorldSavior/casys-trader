"""Runtime adapter for batch and queue decision dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator

from trader.agent.protocol.types import Decision
from trader.application.decide import planner_batch, queue_dispatch, recent_decisions
from trader.application.decide.learning_context import filter_applied_learning_ids
from trader.application.execute.cycle_decision import DecisionExecutionState
from trader.application.record import plan_review
from trader.planning.protocols import SchedulerLike


@dataclass
class ModelCallCounter:
    """Mutable counter shared with recorder status callbacks during streaming."""

    used: int = 0


@dataclass(frozen=True)
class DecisionDispatchRequest:
    queue_decide_enabled: bool
    task_ledger: object | None
    decidable: list[str]
    mandate: str
    memory: str
    shared_context: dict
    triggers_by_symbol: dict[str, list[dict]]
    wake_reasons_by_symbol: dict[str, list[dict]]
    analysis_bars_by_symbol: dict[str, list]
    analysis_timeframe_by_symbol: dict[str, str]
    analysis_symbols: list[str]
    data_age_by_symbol: dict[str, float]
    execution_eligibility: dict[str, dict]
    now: datetime
    scheduler: SchedulerLike | None
    plan_store: object
    decision_ledger_store: object
    decision_timeout_s: int
    agent_tools_enabled: bool
    cycle_id: str
    symbols_to_decide: list[str]
    broker: object
    execution_state: DecisionExecutionState
    execution_context: object
    runtime_interval: str
    runtime_lookback: str
    max_context_requests_per_symbol: int
    max_indicators_per_request: int
    max_model_calls: int
    decision_batch_size: int
    decision_batch_parallelism: int
    learnings_recall_provider: Callable[[dict], dict] | None
    indicator_request_resolver: Callable
    event_appender: Callable[..., None]
    opening_intents: frozenset[str] | set[str]
    model_call_counter: ModelCallCounter
    now_fn: Callable[[], float]
    company_context_by_symbol: dict[str, dict] = field(default_factory=dict)
    mandate_context_by_symbol: dict[str, dict] = field(default_factory=dict)
    learning_feedback_provider: Callable[[list[str]], dict[str, dict]] | None = None


@dataclass(frozen=True)
class DecisionDispatchResult:
    decisions_by_symbol: dict[str, Decision]
    model_calls_used: int
    undecided_symbols: set[str]
    streamed_decision_symbols: set[str]
    execution_state: DecisionExecutionState


def dispatch_decisions(
    request: DecisionDispatchRequest,
    *,
    resolve_decision_for_routing: Callable[..., Decision],
    execute_decision: Callable[..., DecisionExecutionState],
    queue_results_iterator: Callable[..., Iterator[tuple[str, Decision | None, int]]]
    | None = None,
    batch_decider: Callable[..., tuple[dict[str, Decision], int]] | None = None,
    last_review_loader: Callable[..., dict[str, dict]] | None = None,
    recent_decisions_loader: Callable[..., dict[str, list]] | None = None,
    active_watches_builder: Callable[..., dict[str, list]] | None = None,
    symbol_facts_builder: Callable[..., dict] | None = None,
) -> DecisionDispatchResult:
    """Dispatch decisions and stream queue reducers before buffered openings."""

    load_last_review = last_review_loader or plan_review.last_review_by_symbol
    if request.queue_decide_enabled and request.task_ledger is not None:
        return _dispatch_via_queue(
            request,
            resolve_decision_for_routing=resolve_decision_for_routing,
            execute_decision=execute_decision,
            queue_results_iterator=(
                queue_results_iterator
                or queue_dispatch.iter_decide_results_via_queue
            ),
            last_review_loader=load_last_review,
            recent_decisions_loader=(
                recent_decisions_loader
                or recent_decisions.recent_decisions_by_symbol
            ),
            active_watches_builder=(
                active_watches_builder
                or planner_batch._active_watch_summaries_by_symbol
            ),
            symbol_facts_builder=(
                symbol_facts_builder or planner_batch.build_symbol_facts
            ),
        )

    decide_batch = batch_decider or planner_batch.batch_decide
    last_review = load_last_review(request.plan_store, request.decidable)
    recent = _load_recent_decisions(
        recent_decisions_loader or recent_decisions.recent_decisions_by_symbol,
        store=request.decision_ledger_store,
        symbols=request.decidable,
    )
    feedback = _feedback_for_context(
        request.learning_feedback_provider,
        _decision_ids_for_context(last_review, recent),
    )
    last_review = plan_review.annotate_learning_feedback(last_review, feedback)
    recent = recent_decisions.annotate_learning_feedback(recent, feedback)
    decisions_by_symbol, model_calls_used = decide_batch(
        decidable=request.decidable,
        mandate=request.mandate,
        memory=request.memory,
        shared_context=request.shared_context,
        triggers_by_symbol=request.triggers_by_symbol,
        wake_reasons_by_symbol=request.wake_reasons_by_symbol,
        tradable_bars_by_symbol=request.analysis_bars_by_symbol,
        tradable_symbols=request.analysis_symbols,
        runtime_interval=request.runtime_interval,
        runtime_lookback=request.runtime_lookback,
        max_context_requests_per_symbol=request.max_context_requests_per_symbol,
        max_indicators_per_request=request.max_indicators_per_request,
        max_model_calls=request.max_model_calls,
        now=request.now,
        data_age_by_symbol=request.data_age_by_symbol,
        sched=request.scheduler,
        last_review_by_symbol=last_review,
        recent_decisions_by_symbol=recent,
        market_context_by_symbol=request.execution_eligibility,
        decision_timeout_s=request.decision_timeout_s,
        decision_batch_size=request.decision_batch_size,
        decision_batch_parallelism=request.decision_batch_parallelism,
        agent_tools_enabled=request.agent_tools_enabled,
        learnings_recall_provider=request.learnings_recall_provider,
        indicator_request_resolver=request.indicator_request_resolver,
        event_appender=request.event_appender,
        bar_timeframe_by_symbol=request.analysis_timeframe_by_symbol,
        company_context_by_symbol=request.company_context_by_symbol,
        mandate_context_by_symbol=request.mandate_context_by_symbol,
    )
    decisions_by_symbol = {
        symbol: filter_applied_learning_ids(
            decision,
            shared_context=request.shared_context,
        )
        for symbol, decision in decisions_by_symbol.items()
    }
    request.model_call_counter.used = model_calls_used
    return DecisionDispatchResult(
        decisions_by_symbol=decisions_by_symbol,
        model_calls_used=model_calls_used,
        undecided_symbols=set(),
        streamed_decision_symbols=set(),
        execution_state=request.execution_state,
    )


def _dispatch_via_queue(
    request: DecisionDispatchRequest,
    *,
    resolve_decision_for_routing: Callable[..., Decision],
    execute_decision: Callable[..., DecisionExecutionState],
    queue_results_iterator: Callable[..., Iterator[tuple[str, Decision | None, int]]],
    last_review_loader: Callable[..., dict[str, dict]],
    recent_decisions_loader: Callable[..., dict[str, list]],
    active_watches_builder: Callable[..., dict[str, list]],
    symbol_facts_builder: Callable[..., dict],
) -> DecisionDispatchResult:
    last_review = last_review_loader(request.plan_store, request.decidable)
    recent = _load_recent_decisions(
        recent_decisions_loader,
        store=request.decision_ledger_store,
        symbols=request.decidable,
    )
    feedback = _feedback_for_context(
        request.learning_feedback_provider,
        _decision_ids_for_context(last_review, recent),
    )
    last_review = plan_review.annotate_learning_feedback(last_review, feedback)
    recent = recent_decisions.annotate_learning_feedback(recent, feedback)
    active_watches = active_watches_builder(
        sched=request.scheduler,
        symbols=request.decidable,
        now=request.now,
    )
    symbol_facts_by_symbol = {
        symbol: {
            "indicator_triggers": request.triggers_by_symbol.get(symbol, []),
            "wake_reasons": request.wake_reasons_by_symbol.get(symbol, []),
            **symbol_facts_builder(
                symbol,
                data_age_by_symbol=request.data_age_by_symbol,
                now=request.now,
                active_watches_by_symbol=active_watches,
                market_context_by_symbol=request.execution_eligibility,
                last_review_by_symbol=last_review,
                recent_decisions_by_symbol=recent,
                bars_by_symbol=request.analysis_bars_by_symbol,
                bar_timeframe_by_symbol=request.analysis_timeframe_by_symbol,
                company_context_by_symbol=request.company_context_by_symbol,
                mandate_context_by_symbol=request.mandate_context_by_symbol,
            ),
        }
        for symbol in request.decidable
    }
    stream_index_by_symbol = {
        symbol: index
        for index, symbol in enumerate(request.symbols_to_decide, start=1)
    }
    decisions_by_symbol: dict[str, Decision] = {}
    undecided_symbols: set[str] = set()
    streamed_decision_symbols: set[str] = set()
    execution_state = request.execution_state

    for symbol, decision, calls in queue_results_iterator(
        ledger=request.task_ledger,
        decidable=request.decidable,
        mandate=request.mandate,
        memory=request.memory,
        shared_context=request.shared_context,
        symbol_facts_by_sym=symbol_facts_by_symbol,
        decision_timeout_s=request.decision_timeout_s,
        agent_tools_enabled=request.agent_tools_enabled,
        cycle_id=request.cycle_id,
        now_fn=request.now_fn,
        symbols_universe=request.analysis_symbols,
    ):
        if decision is None:
            undecided_symbols.add(symbol)
            continue
        request.model_call_counter.used += calls
        filtered_decision = filter_applied_learning_ids(
            decision,
            shared_context=request.shared_context,
        )
        routed_decision = resolve_decision_for_routing(
            symbol=symbol,
            decision=filtered_decision,
            broker=request.broker,
        )
        decisions_by_symbol[symbol] = routed_decision
        if routed_decision.intent in request.opening_intents:
            continue

        execution_state = execute_decision(
            sym=symbol,
            index=stream_index_by_symbol.get(
                symbol, len(streamed_decision_symbols) + 1
            ),
            total=len(request.symbols_to_decide),
            decision=routed_decision,
            state=execution_state,
            ctx=request.execution_context,
        )
        streamed_decision_symbols.add(symbol)

    return DecisionDispatchResult(
        decisions_by_symbol=decisions_by_symbol,
        model_calls_used=request.model_call_counter.used,
        undecided_symbols=undecided_symbols,
        streamed_decision_symbols=streamed_decision_symbols,
        execution_state=execution_state,
    )


def _decision_ids_for_context(
    last_review_by_symbol: dict[str, dict],
    recent_by_symbol: dict[str, list[dict]],
) -> list[str]:
    ids = [
        str(review.get("decision_id"))
        for review in last_review_by_symbol.values()
        if review.get("decision_id")
    ]
    ids.extend(
        str(row.get("decision_id"))
        for rows in recent_by_symbol.values()
        for row in rows
        if row.get("decision_id")
    )
    return list(dict.fromkeys(ids))


def _feedback_for_context(
    provider: Callable[[list[str]], dict[str, dict]] | None,
    decision_ids: list[str],
) -> dict[str, dict]:
    if provider is None or not decision_ids:
        return {}
    try:
        payload = provider(decision_ids)
    except Exception:  # noqa: BLE001 - cockpit feedback is advisory only
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_recent_decisions(
    loader: Callable[..., dict[str, list]],
    *,
    store: object,
    symbols: list[str],
) -> dict[str, list]:
    """Recent-decision context is advisory; retain legacy batch adapters without a ledger."""

    try:
        payload = loader(store, symbols=symbols)
    except (AttributeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "DecisionDispatchRequest",
    "DecisionDispatchResult",
    "ModelCallCounter",
    "dispatch_decisions",
]
