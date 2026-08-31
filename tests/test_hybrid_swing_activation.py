"""Activation swing/intraday lente — cap 25k, replay stale, cadence, funnel.

Couvre les 8 cas demandés au niveau des fonctions pures / services existants.
Le replay persistant réutilise ``llm_gate_last_seen.wake_fingerprints_json``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from trader.application.cycle.infra_holds import quiet_gate_decisions
from trader.domain.contracts import Order
from trader.domain.execution.risk_gate import RiskGate
from trader.domain.planning import relevance_gate
from trader.domain.risk import RiskLimits
from trader.reporting.read_models.strategy_funnel import compute_strategy_funnel


NOW = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
BAR_A = "2026-06-11T14:15:00+00:00"
BAR_B = "2026-06-11T14:30:00+00:00"
PENDING = {
    relevance_gate.STALE_REVIEW_FINGERPRINT_KEY: relevance_gate.STALE_REVIEW_PENDING
}


class _WakeSource:
    def __init__(self, symbols: set[str] | None = None) -> None:
        self._symbols = symbols or set()

    def symbols_with_wake(self) -> set[str]:
        return set(self._symbols)


def _needs(**kwargs) -> tuple[bool, str]:
    base = dict(
        agent_requested_wake=False,
        has_trigger=False,
        has_position=False,
        family_regime_strong=False,
        stretched=False,
        aligned=None,
        sig=None,
        hours_since_last_llm=0.25,
        last_wake_reasons=None,
        session_open=True,
        execution_enabled=False,
        has_hot_setup=False,
        current_runtime_bar_ts=None,
        current_trigger_fingerprint=None,
    )
    base.update(kwargs)
    return relevance_gate.symbol_needs_llm(**base)


def _gate() -> RiskGate:
    return RiskGate(
        RiskLimits(
            max_position_value=25_000.0,
            max_gross_exposure=100_000.0,
            max_order_value=50_000.0,
            min_equity=50_000.0,
            max_risk_per_trade_pct=0.01,
            confidence_gate_enabled=False,
        )
    )


def _quiet_gate(**overrides):
    payload = dict(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(minutes=20)},
        cockpit={"cols": ["s", "st", "sig"], "rows": [["SPY", False, []]]},
        regime_families={},
        active_families={},
        wake_source=_WakeSource(),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={"SPY": "yfinance"},
        last_wake_reasons={("/tmp/state", "SPY"): ("periodic_review",)},
        last_wake_fingerprints={("/tmp/state", "SPY"): dict(PENDING)},
        execution_eligibility={
            "SPY": {
                "execution": {
                    "enabled": True,
                    "reason": "tradable",
                    "last_runtime_bar_ts": BAR_B,
                }
            }
        },
        hot_setup_symbols=set(),
        runtime_bar_ts_by_symbol={"SPY": BAR_B},
    )
    payload.update(overrides)
    return quiet_gate_decisions(**payload)


def test_paper_position_cap_is_25000_and_risk_pct_stays_one_percent() -> None:
    raw = yaml.safe_load(Path("config/risk.yaml").read_text(encoding="utf-8"))
    assert raw["max_position_value"] == 25_000
    assert raw["max_risk_per_trade_pct"] == 0.01
    assert raw["max_order_value"] == 50_000


def test_cap_25k_refuse_une_augmentation_au_dessus_du_plafond() -> None:
    verdict = _gate().check(
        Order("SPY", "BUY", 60.0, rationale="scale-in"),
        100.0,
        current_position_value=20_000.0,
        gross_exposure=20_000.0,
        equity=100_000.0,
        allow_risk_reduction=False,
    )
    assert verdict.approved is False
    assert verdict.code == "position_value_exceeded"


def test_cap_25k_autorise_la_reduction_meme_si_la_position_reste_au_dessus() -> None:
    """Position grand-père > 25k : une réduction partielle ne liquide pas et n'est pas refusée."""
    verdict = _gate().check(
        Order("SPY", "SELL", 50.0, rationale="reduce"),
        100.0,
        current_position_value=40_000.0,
        gross_exposure=40_000.0,
        equity=100_000.0,
        allow_risk_reduction=True,
    )
    assert verdict.approved is True
    still_increasing = _gate().check(
        Order("SPY", "BUY", 10.0, rationale="add"),
        100.0,
        current_position_value=40_000.0,
        gross_exposure=40_000.0,
        equity=100_000.0,
        allow_risk_reduction=False,
    )
    assert still_increasing.approved is False
    assert still_increasing.code == "position_value_exceeded"


def test_stale_review_then_fresh_open_replays_once() -> None:
    assert _needs(
        hours_since_last_llm=0.2,
        last_wake_fingerprints=PENDING,
        execution_enabled=True,
        session_open=True,
    ) == (True, "fresh_after_stale_review")


def test_fresh_after_stale_review_does_not_repeat_on_next_poll() -> None:
    assert _needs(
        hours_since_last_llm=0.2,
        last_wake_fingerprints={
            relevance_gate.REVIEWED_BAR_FINGERPRINT_KEY: relevance_gate.reviewed_bar_fingerprint(BAR_B)
        },
        execution_enabled=True,
        session_open=True,
        current_runtime_bar_ts=BAR_B,
    ) == (False, "quiet")
    assert _needs(
        hours_since_last_llm=0.2,
        last_wake_fingerprints={},
        execution_enabled=True,
        session_open=True,
    ) == (False, "quiet")


def test_session_closed_or_still_stale_does_not_replay() -> None:
    assert _needs(
        hours_since_last_llm=0.2,
        last_wake_fingerprints=PENDING,
        execution_enabled=False,
        session_open=False,
    ) == (False, "stale_replay_wait")


def test_explicit_closed_session_review_preserves_pending_fresh_replay() -> None:
    closed = _quiet_gate(
        wake_source=_WakeSource({"SPY"}),
        execution_eligibility={
            "SPY": {
                "execution": {
                    "enabled": False,
                    "reason": "session_closed",
                    "last_runtime_bar_ts": BAR_B,
                }
            }
        },
    )

    assert closed.kept_symbols == ["SPY"]
    assert closed.reasons["SPY"] == "agent_wake"
    assert closed.persistent_fingerprints["SPY"][
        relevance_gate.STALE_REVIEW_FINGERPRINT_KEY
    ] == relevance_gate.STALE_REVIEW_PENDING

    fresh = _quiet_gate(
        last_wake_fingerprints={
            ("/tmp/state", "SPY"): closed.persistent_fingerprints["SPY"]
        }
    )
    assert fresh.kept_symbols == ["SPY"]
    assert fresh.reasons["SPY"] == "fresh_after_stale_review"
    assert _needs(
        hours_since_last_llm=0.2,
        last_wake_fingerprints=PENDING,
        execution_enabled=False,
        session_open=True,
    ) == (False, "stale_replay_wait")


def test_quiet_gate_replays_once_when_runtime_becomes_fresh_and_open() -> None:
    replay = _quiet_gate()
    assert replay.kept_symbols == ["SPY"]
    assert replay.reasons["SPY"] == "fresh_after_stale_review"
    assert relevance_gate.STALE_REVIEW_FINGERPRINT_KEY not in replay.persistent_fingerprints["SPY"]

    still_stale = _quiet_gate(
        execution_eligibility={
            "SPY": {
                "execution": {
                    "enabled": False,
                    "reason": "runtime_stale",
                    "last_runtime_bar_ts": BAR_A,
                }
            }
        },
        runtime_bar_ts_by_symbol={"SPY": BAR_A},
    )
    assert still_stale.kept_symbols == []
    assert still_stale.entries[0]["decision_source"] == "infra"
    assert still_stale.entries[0]["model_called"] is False

    session_closed = _quiet_gate(
        execution_eligibility={
            "SPY": {
                "execution": {
                    "enabled": False,
                    "reason": "session_closed",
                    "last_runtime_bar_ts": BAR_B,
                }
            }
        }
    )
    assert session_closed.kept_symbols == []


def test_quiet_gate_does_not_replay_on_the_poll_after_fresh_review() -> None:
    second_poll = _quiet_gate(
        last_wake_fingerprints={
            ("/tmp/state", "SPY"): {
                relevance_gate.REVIEWED_BAR_FINGERPRINT_KEY: relevance_gate.reviewed_bar_fingerprint(BAR_B)
            }
        }
    )
    assert second_poll.kept_symbols == []
    assert second_poll.gated_symbols == ["SPY"]


def test_d7b_and_risk_gate_semantics_stay_fail_closed() -> None:
    """D7B n'est pas un second veto LLM : le fusible reste RiskGate.check()."""
    gate = _gate()
    opening = gate.check(
        Order("SPY", "BUY", 10.0, rationale="armed execute"),
        100.0,
        current_position_value=0.0,
        gross_exposure=0.0,
        equity=100_000.0,
    )
    assert opening.approved is True
    oversized = gate.check(
        Order("SPY", "BUY", 300.0, rationale="armed execute"),
        100.0,
        current_position_value=0.0,
        gross_exposure=0.0,
        equity=100_000.0,
    )
    assert oversized.approved is False
    assert oversized.code == "position_value_exceeded"
    confidence = gate.check_confidence(0.05, 0.01)
    assert confidence.approved is True  # paper : gate confiance off, RiskGate notionnel inchangé


def test_funnel_separates_llm_watches_triggers_orders_and_fills() -> None:
    funnel = compute_strategy_funnel(
        decisions=[
            {
                "cycle_ts": "2026-06-11T14:00:00+00:00",
                "decision_source": "infra",
                "model_called": False,
                "action": "HOLD",
                "reason": "stale_market_data",
                "executed": False,
                "decision_id": "infra-1",
            },
            {
                "cycle_ts": "2026-06-11T14:05:00+00:00",
                "decision_source": "llm",
                "model_called": True,
                "action": "HOLD",
                "rationale": "Attendre la confirmation de cassure.",
                "executed": False,
                "decision_id": "llm-watch-simple",
                "indicator_watch": {
                    "id": "SPY:w-simple",
                    "on_trigger": "WAKE",
                },
            },
            {
                "cycle_ts": "2026-06-11T14:10:00+00:00",
                "decision_source": "llm",
                "model_called": True,
                "action": "BUY",
                "rationale": "Entrée immédiate dans le sens de la thèse.",
                "executed": True,
                "decision_id": "llm-buy",
            },
            {
                "cycle_ts": "2026-06-11T14:15:00+00:00",
                "decision_source": "llm",
                "model_called": True,
                "action": "HOLD",
                "rationale": "Armer l'ordre seulement après confirmation.",
                "executed": False,
                "decision_id": "llm-watch-armed",
                "indicator_watch": {
                    "id": "SPY:w-armed",
                    "on_trigger": "EXECUTE_ORDER",
                    "order": {"action": "BUY", "qty": 5.0},
                },
            },
            {
                "cycle_ts": "2026-06-11T14:20:00+00:00",
                "decision_source": "armed_plan",
                "model_called": False,
                "action": "BUY",
                "reason": "ok",
                "executed": True,
                "decision_id": "armed-buy",
            },
        ],
        events=[
            {
                "ts": "2026-06-11T14:06:00+00:00",
                "event": "indicator_watch_created",
                "watch_id": "SPY:w-simple",
            },
            {
                "ts": "2026-06-11T14:16:00+00:00",
                "event": "armed_plan_created",
                "watch_id": "SPY:w-armed",
            },
            {
                "ts": "2026-06-11T14:19:00+00:00",
                "event": "indicator_watch_triggered",
                "watch_id": "SPY:w-simple",
            },
            {
                "ts": "2026-06-11T14:19:10+00:00",
                "event": "indicator_watch_triggered",
                "watch_id": "SPY:w-armed",
            },
            {
                "ts": "2026-06-11T14:19:30+00:00",
                "event": "exit_watch_triggered",
                "watch_id": "SPY:exit-1",
            },
        ],
        fills=[
            {
                "ts": "2026-06-11T14:10:01+00:00",
                "symbol": "SPY",
                "quantity": 10,
                "decision_id": "llm-buy",
                "process_instance_id": "process-simple",
                "attempt_id": "attempt-simple",
            },
            {
                "ts": "2026-06-11T14:12:00+00:00",
                "symbol": "SPY",
                "quantity": 1,
            },
        ],
        process_events=[
            {
                "ts": "2026-06-11T14:00:00+00:00",
                "process_instance_id": "coverage-baseline",
                "attempt_id": "coverage-baseline",
                "work_object_key": "SPY",
                "caused_by": [],
                "effect_refs": [],
            },
            {
                "ts": "2026-06-11T14:18:59+00:00",
                "process_instance_id": "process-simple",
                "attempt_id": "attempt-simple",
                "work_object_key": "SPY",
                "caused_by": [
                    {
                        "type": "indicator_trigger",
                        "watch_id": "SPY:w-simple",
                    }
                ],
                "effect_refs": [],
            },
            {
                "ts": "2026-06-11T14:20:01+00:00",
                "process_instance_id": "process-simple",
                "attempt_id": "attempt-simple",
                "work_object_key": "SPY",
                "caused_by": [],
                "effect_refs": [
                    {
                        "type": "decision",
                        "decision_id": "llm-buy",
                        "process_instance_id": "process-simple",
                        "attempt_id": "attempt-simple",
                    }
                ],
            },
            {
                "ts": "2026-06-11T14:19:05+00:00",
                "process_instance_id": "process-armed",
                "attempt_id": "attempt-armed",
                "work_object_key": "SPY",
                "caused_by": [
                    {
                        "type": "indicator_trigger",
                        "watch_id": "SPY:w-armed",
                    }
                ],
                "effect_refs": [
                    {
                        "type": "decision",
                        "decision_id": "armed-buy",
                        "process_instance_id": "process-armed",
                        "attempt_id": "attempt-armed",
                    }
                ],
            },
        ],
    )
    assert funnel.as_dict() == {
        "llm_reviews": 3,
        "infra_holds": 1,
        "watches_created": 1,
        "watches_armed": 1,
        "watches_triggered": 2,
        "watches_expired_untriggered": 0,
        "watches_cancelled": 0,
        "watches_superseded": 0,
        "watches_pending_or_unresolved": 0,
        "external_indicator_triggers": 0,
        "exit_watch_triggers": 1,
        "unjoinable_watch_creations": 0,
        "buy_sell_proposed": 1,
        "orders_submitted": 1,
        "fills": 1,
        "non_decision_fills": 1,
        "causal_linkage_available": True,
        "causal_coverage_start": "2026-06-11T14:00:00+00:00",
        "trigger_process_attempts": 2,
        "trigger_decisions": 2,
        "trigger_holds": 0,
        "trigger_trade_decisions": 2,
        "trigger_orders_submitted": 1,
        "trigger_fills": 1,
        "triggered_watches_with_fill": 1,
        "trigger_without_process": 0,
        "trigger_process_without_decision": 0,
        "triggered_cancelled": 0,
        "expiry_review_decisions": 0,
        "expiry_review_fills": 0,
        "direct_trade_decisions": 0,
        "direct_fills": 0,
    }


def test_hot_setup_and_position_review_at_most_1h_during_session() -> None:
    assert relevance_gate.HOT_REVIEW_MAX_HOURS == 1.0
    assert relevance_gate.CALM_REVIEW_MAX_HOURS == 4.0
    assert _needs(
        has_position=True,
        hours_since_last_llm=0.99,
        session_open=True,
    ) == (False, "position_debounce")
    assert _needs(
        has_position=True,
        hours_since_last_llm=1.0,
        session_open=True,
    ) == (True, "position")
    assert _needs(
        has_hot_setup=True,
        hours_since_last_llm=0.99,
        session_open=True,
    ) == (False, "quiet")
    assert _needs(
        has_hot_setup=True,
        hours_since_last_llm=1.0,
        session_open=True,
    ) == (True, "hot_setup")


def test_calm_symbol_does_not_review_before_4h() -> None:
    assert _needs(hours_since_last_llm=3.99, session_open=True) == (False, "quiet")
    assert _needs(hours_since_last_llm=4.0, session_open=True) == (True, "periodic_review")
    assert _needs(
        has_position=True,
        hours_since_last_llm=1.0,
        session_open=False,
    ) == (False, "position_debounce")
    assert _needs(
        has_hot_setup=True,
        hours_since_last_llm=1.0,
        session_open=False,
    ) == (False, "quiet")
    assert _needs(
        has_position=True,
        hours_since_last_llm=4.0,
        session_open=False,
    ) == (True, "position")


def test_no_price_during_open_session_keeps_hot_cadence_and_arms_replay() -> None:
    execution = {
        "enabled": False,
        "reason": "no_price",
        "session_open": True,
    }

    assert relevance_gate.session_is_open(execution) is True
    assert relevance_gate.should_mark_stale_review_pending(execution) is True
    assert _needs(
        has_position=True,
        hours_since_last_llm=1.0,
        session_open=relevance_gate.session_is_open(execution),
        execution_enabled=False,
    ) == (True, "position")


def test_same_15m_bar_does_not_trigger_multiple_reviews() -> None:
    trigger_a = relevance_gate.reviewed_trigger_fingerprint(
        [f"SPY:w1@{BAR_A}"]
    )
    trigger_b = relevance_gate.reviewed_trigger_fingerprint(
        [f"SPY:w1@{BAR_B}"]
    )
    fingerprints = {
        relevance_gate.REVIEWED_TRIGGER_FINGERPRINT_KEY: trigger_a
    }
    assert _needs(
        has_trigger=True,
        hours_since_last_llm=0.05,
        last_wake_fingerprints=fingerprints,
        current_runtime_bar_ts=BAR_A,
        current_trigger_fingerprint=trigger_a,
        execution_enabled=True,
    ) == (False, "same_runtime_bar")
    assert _needs(
        has_trigger=True,
        hours_since_last_llm=0.05,
        last_wake_fingerprints=fingerprints,
        current_runtime_bar_ts=BAR_B,
        current_trigger_fingerprint=trigger_b,
        execution_enabled=True,
    ) == (True, "trigger")
    distinct_watch_same_bar = relevance_gate.reviewed_trigger_fingerprint(
        [f"SPY:w2@{BAR_A}"]
    )
    assert _needs(
        has_trigger=True,
        hours_since_last_llm=0.05,
        last_wake_fingerprints=fingerprints,
        current_runtime_bar_ts=BAR_A,
        current_trigger_fingerprint=distinct_watch_same_bar,
        execution_enabled=True,
    ) == (True, "trigger")
    assert _needs(
        agent_requested_wake=True,
        hours_since_last_llm=0.05,
        last_wake_fingerprints=fingerprints,
        current_runtime_bar_ts=BAR_A,
    ) == (True, "agent_wake")
