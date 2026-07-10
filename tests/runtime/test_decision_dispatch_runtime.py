from __future__ import annotations

from datetime import datetime, timezone

from trader.agent.protocol.types import Decision
from trader.application.execute.cycle_decision import DecisionExecutionState
from trader.runtime.decision_dispatch_runtime import (
    DecisionDispatchRequest,
    ModelCallCounter,
    dispatch_decisions,
)


NOW = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)


def _request(**overrides) -> DecisionDispatchRequest:
    payload = {
        "queue_decide_enabled": False,
        "task_ledger": None,
        "decidable": ["SPY"],
        "mandate": "mandate",
        "memory": "memory",
        "shared_context": {"shared": True},
        "triggers_by_symbol": {"SPY": [{"trigger": True}]},
        "wake_reasons_by_symbol": {"SPY": [{"reason": "wake"}]},
        "analysis_bars_by_symbol": {"SPY": ["bars"]},
        "analysis_timeframe_by_symbol": {"SPY": "15m"},
        "analysis_symbols": ["SPY"],
        "data_age_by_symbol": {"SPY": 1.0},
        "execution_eligibility": {"SPY": {"execution": {"enabled": True}}},
        "now": NOW,
        "scheduler": None,
        "plan_store": object(),
        "decision_ledger_store": object(),
        "decision_timeout_s": 30,
        "agent_tools_enabled": False,
        "cycle_id": NOW.isoformat(),
        "symbols_to_decide": ["SPY"],
        "broker": object(),
        "execution_state": DecisionExecutionState(snap="initial", gross=100.0),
        "execution_context": object(),
        "runtime_interval": "15m",
        "runtime_lookback": "5d",
        "max_context_requests_per_symbol": 2,
        "max_indicators_per_request": 4,
        "max_model_calls": 25,
        "decision_batch_size": 5,
        "decision_batch_parallelism": 3,
        "learnings_recall_provider": None,
        "indicator_request_resolver": lambda *_args, **_kwargs: {},
        "event_appender": lambda *_args, **_kwargs: None,
        "opening_intents": frozenset({"OPEN_LONG", "OPEN_SHORT", "FLIP"}),
        "model_call_counter": ModelCallCounter(),
        "now_fn": lambda: 1.0,
    }
    payload.update(overrides)
    return DecisionDispatchRequest(**payload)


def test_batch_dispatch_uses_application_planner_and_updates_shared_counter() -> None:
    decision = Decision.hold("SPY", "wait")
    captured: dict[str, object] = {}
    counter = ModelCallCounter()
    plan_store = object()

    def load_last_review(received_store, received_symbols):
        assert received_store is plan_store
        assert received_symbols == ["SPY"]
        return {"SPY": {"verdict": "intact"}}

    def batch_decider(**kwargs):
        captured.update(kwargs)
        return {"SPY": decision}, 4

    result = dispatch_decisions(
        _request(
            plan_store=plan_store,
            model_call_counter=counter,
        ),
        resolve_decision_for_routing=lambda **_kwargs: None,
        execute_decision=lambda **_kwargs: None,
        batch_decider=batch_decider,
        last_review_loader=load_last_review,
    )

    assert result.decisions_by_symbol == {"SPY": decision}
    assert result.model_calls_used == 4
    assert result.undecided_symbols == set()
    assert result.streamed_decision_symbols == set()
    assert result.execution_state.snap == "initial"
    assert counter.used == 4
    assert captured["decidable"] == ["SPY"]
    assert captured["last_review_by_symbol"] == {
        "SPY": {"verdict": "intact"}
    }
    assert captured["tradable_bars_by_symbol"] == {"SPY": ["bars"]}
    assert captured["bar_timeframe_by_symbol"] == {"SPY": "15m"}


def test_queue_dispatch_streams_reducers_and_buffers_openings() -> None:
    reduce_decision = Decision(
        symbol="REDUCE",
        action="HOLD",
        quantity=0.0,
        confidence=0.8,
        rationale="reduce",
        intent="REDUCE",
    )
    open_decision = Decision(
        symbol="OPEN",
        action="BUY",
        quantity=1.0,
        confidence=0.9,
        rationale="open",
        intent="OPEN_LONG",
    )
    counter = ModelCallCounter()
    trace: list[tuple] = []

    def queue_results_iterator(**kwargs):
        facts = kwargs["symbol_facts_by_sym"]
        assert facts["REDUCE"] == {
            "indicator_triggers": [{"trigger": "REDUCE"}],
            "wake_reasons": [{"wake": "REDUCE"}],
            "base": "REDUCE",
        }
        yield "REDUCE", reduce_decision, 2
        yield "OPEN", open_decision, 1
        yield "MISSING", None, 0

    def resolve_decision_for_routing(*, symbol, decision, broker):
        trace.append(("resolve", symbol, broker))
        return decision

    def execute_decision(*, sym, index, total, decision, state, ctx):
        trace.append(
            (
                "execute",
                sym,
                index,
                total,
                decision.intent,
                state.gross,
                ctx,
                counter.used,
            )
        )
        state.gross = 40.0
        state.snap = "after-reducer"
        return state

    broker = object()
    execution_context = object()
    request = _request(
        queue_decide_enabled=True,
        task_ledger=object(),
        decidable=["REDUCE", "OPEN", "MISSING"],
        symbols_to_decide=["OPEN", "REDUCE", "MISSING"],
        broker=broker,
        execution_context=execution_context,
        model_call_counter=counter,
        triggers_by_symbol={
            symbol: [{"trigger": symbol}]
            for symbol in ("REDUCE", "OPEN", "MISSING")
        },
        wake_reasons_by_symbol={
            symbol: [{"wake": symbol}]
            for symbol in ("REDUCE", "OPEN", "MISSING")
        },
        analysis_bars_by_symbol={
            symbol: [symbol]
            for symbol in ("REDUCE", "OPEN", "MISSING")
        },
        analysis_timeframe_by_symbol={
            symbol: "15m"
            for symbol in ("REDUCE", "OPEN", "MISSING")
        },
        analysis_symbols=["MISSING", "OPEN", "REDUCE"],
    )

    result = dispatch_decisions(
        request,
        resolve_decision_for_routing=resolve_decision_for_routing,
        execute_decision=execute_decision,
        queue_results_iterator=queue_results_iterator,
        last_review_loader=lambda *_args, **_kwargs: {},
        recent_decisions_loader=lambda *_args, **_kwargs: {},
        active_watches_builder=lambda **_kwargs: {},
        symbol_facts_builder=lambda symbol, **_kwargs: {"base": symbol},
    )

    assert result.decisions_by_symbol == {
        "REDUCE": reduce_decision,
        "OPEN": open_decision,
    }
    assert result.model_calls_used == 3
    assert result.undecided_symbols == {"MISSING"}
    assert result.streamed_decision_symbols == {"REDUCE"}
    assert result.execution_state.snap == "after-reducer"
    assert result.execution_state.gross == 40.0
    assert counter.used == 3
    assert trace == [
        ("resolve", "REDUCE", broker),
        (
            "execute",
            "REDUCE",
            2,
            3,
            "REDUCE",
            100.0,
            execution_context,
            2,
        ),
        ("resolve", "OPEN", broker),
    ]
