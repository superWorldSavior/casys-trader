"""Tests — recent_decisions_by_symbol (issue #4, push) : groupage, filtre, compactage."""
import json
from dataclasses import replace
from datetime import datetime, timezone

from trader.agent.protocol.parsing import parse_decision
from trader.application.cycle.schedule import apply_decision_schedule
from trader.application.decide.recent_decisions import recent_decisions_by_symbol
from trader.application.record.decision_entries import build_decision_entry
from trader.application.record.decision_ledger_rows import build_decision_row
from trader.domain.planning.indicator_watch import build_indicator_watch
from trader.planning.scheduler import Scheduler


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows
        self.read_calls = 0

    def read_all(self, *, symbol=None, limit=None):
        self.read_calls += 1
        rows = [r for r in self._rows if symbol is None or r.get("symbol") == symbol]
        if limit is not None and limit >= 0:
            rows = rows[-limit:]
        return rows


def _row(symbol, action, rationale="ok", **over):
    base = {
        "symbol": symbol, "cycle_ts": f"t-{action}", "action": action,
        "intent": action, "confidence": 0.7, "decision_reason_code": "LLM_SIGNAL",
        "executed": True, "reason": None, "llm_error": None, "rationale": rationale,
        "decision": {"huge": "blob"}, "market_snapshot": {"x": 1}, "portfolio_snapshot": {"y": 2},
    }
    base.update(over)
    return base


def test_groupe_par_symbole_en_une_seule_lecture():
    store = _FakeStore([_row("AAPL", "BUY"), _row("MSFT", "SELL"), _row("AAPL", "HOLD")])

    out = recent_decisions_by_symbol(store, symbols=["AAPL", "MSFT"])

    assert store.read_calls == 1  # une lecture, pas une par symbole
    assert {r["action"] for r in out["AAPL"]} == {"BUY", "HOLD"}
    assert [r["action"] for r in out["MSFT"]] == ["SELL"]


def test_plus_recent_dabord_et_limite():
    store = _FakeStore([_row("AAPL", "A"), _row("AAPL", "B"), _row("AAPL", "C")])

    out = recent_decisions_by_symbol(store, symbols=["AAPL"], limit=2)

    assert [r["action"] for r in out["AAPL"]] == ["C", "B"]  # 2 plus récentes, récent d'abord


def test_exclut_hold_synthetiques_sans_manger_le_budget():
    store = _FakeStore([
        _row("AAPL", "BUY", llm_error=None),
        _row("AAPL", "HOLD", llm_error="timeout"),
        _row("AAPL", "HOLD", llm_error="rate_limited"),
        _row("AAPL", "SELL", llm_error=None),
    ])

    out = recent_decisions_by_symbol(store, symbols=["AAPL"], limit=2)

    assert [r["action"] for r in out["AAPL"]] == ["SELL", "BUY"]


def test_exclut_hold_infra_sans_evincer_effet_plan_arme():
    store = _FakeStore([
        _row("AAPL", "HOLD", decision_source="llm", model_called=True),
        _row(
            "AAPL",
            "HOLD",
            rationale="quiet_gate",
            decision_source="infra",
            model_called=False,
        ),
        _row(
            "AAPL",
            "HOLD",
            rationale="armed_plan:AAPL:breakout",
            decision_source="armed_plan",
            source="armed_plan",
            model_called=False,
        ),
    ])

    out = recent_decisions_by_symbol(store, symbols=["AAPL"], limit=2)

    assert [r["decision_reason_code"] for r in out["AAPL"]] == [
        "LLM_SIGNAL",
        "LLM_SIGNAL",
    ]
    assert [r["rationale"] for r in out["AAPL"]] == [
        "armed_plan:AAPL:breakout",
        "ok",
    ]


def test_exclut_hold_infra_legacy_identifie_uniquement_par_reason():
    store = _FakeStore([
        _row("AAPL", "BUY"),
        _row(
            "AAPL",
            "HOLD",
            rationale="fallback historique",
            reason="stale_market_data",
            decision_source=None,
            model_called=None,
            llm_error=None,
        ),
        _row("AAPL", "SELL"),
    ])

    out = recent_decisions_by_symbol(store, symbols=["AAPL"], limit=2)

    assert [row["action"] for row in out["AAPL"]] == ["SELL", "BUY"]


def test_conserve_direction_et_resume_watch_pour_continuite():
    store = _FakeStore([
        _row(
            "NOC",
            "HOLD",
            executed=False,
            opportunity_side="long",
            decision={
                "opportunity_side": "long",
                "indicator_watch": {
                    "id": "NOC:reclaim",
                    "on_trigger": "WAKE",
                    "created_at": "2026-08-13T13:50:01+00:00",
                    "expires_at": "2026-08-13T17:50:01+00:00",
                    "logic": "all",
                    "rationale": "Attendre le reclaim avant d'évaluer le long.",
                    "conditions": [
                        {
                            "indicator": "z_score",
                            "op": ">=",
                            "value": 0.2,
                            "timeframe": "15m",
                        }
                    ],
                },
            },
        )
    ])

    recent = recent_decisions_by_symbol(store, symbols=["NOC"])["NOC"][0]

    assert recent["opportunity_side"] == "long"
    assert recent["indicator_watch"] == {
        "id": "NOC:reclaim",
        "kind": "wake",
        "on_trigger": "WAKE",
        "created_at": "2026-08-13T13:50:01+00:00",
        "rationale": "Attendre le reclaim avant d'évaluer le long.",
        "expires_at": "2026-08-13T17:50:01+00:00",
        "logic": "all",
        "conditions": [
            {
                "indicator": "z_score",
                "op": ">=",
                "value": 0.2,
                "timeframe": "15m",
            }
        ],
    }


def test_continuite_watch_de_la_reponse_modele_au_prochain_prompt(tmp_path):
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    raw_decision = {
        "symbol": "AIR.PA",
        "confidence": 0.72,
        "rationale": "Attendre le reclaim horaire avant l'entree.",
        "opportunity_side": "long",
        "decision_reason_code": "WATCH_ARMED",
        "calls": [
            {
                "tool": "propose_indicator_watch",
                "args": {
                    "logic": "all",
                    "ttl_minutes": 180,
                    "on_trigger": "EXECUTE_ORDER",
                    "conditions": [
                        {
                            "symbol": "AIR.PA",
                            "indicator": "return",
                            "op": ">",
                            "value": 0.01,
                            "interval": "1h",
                            "window": 24,
                            "as_of": "latest",
                        }
                    ],
                    "order": {
                        "direction": "long",
                        "qty": 2,
                        "confidence": 0.72,
                        "exit": {"stop": {"type": "price", "price": 155.0}},
                            "evaluation_id": "tpe_test",
                        "rationale": "Executer ce plan si le reclaim arrive.",
                    },
                },
            }
        ],
    }
    decision = replace(
        parse_decision(json.dumps(raw_decision), "AIR.PA"),
        llm_provider="openai",
        llm_model="luna",
    )
    built = build_indicator_watch(decision.indicator_watch, owner_symbol="AIR.PA", now=now)
    assert built.watch is not None
    entry = build_decision_entry(
        symbol="AIR.PA",
        decision=decision,
        effective_quantity=0.0,
        next_wake_in_minutes=None,
        next_wake_event_iso=None,
        armed_plan_id=None,
        armed_plan_order=None,
        runtime_data_source="test",
    )
    entry.update({"executed": False, "reason": "hold"})
    apply_decision_schedule(
        sched=Scheduler(tmp_path / "scheduler.json"),
        sym="AIR.PA",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=[],
        pending_indicator_watch=built.watch,
        entry=entry,
    )
    row = build_decision_row(
        {
            "ts": now.isoformat(),
            "dry_run": False,
            "symbols_due": ["AIR.PA"],
            "prices": {"AIR.PA": 160.0},
            "portfolio": {"cash": 100_000.0, "equity": 100_000.0},
            "stale_market_data": {},
            "model_calls_used": 1,
        },
        entry,
        sequence=0,
    )

    watch = recent_decisions_by_symbol(_FakeStore([row]), symbols=["AIR.PA"])["AIR.PA"][0][
        "indicator_watch"
    ]

    assert watch["created_at"] == "2026-07-02T10:00:00+00:00"
    assert watch["rationale"] == "Attendre le reclaim horaire avant l'entree."
    assert watch["conditions"] == [
        {
            "symbol": "AIR.PA",
            "indicator": "return",
            "op": ">",
            "value": 0.01,
            "timeframe": "1h",
            "source_interval": "1h",
            "lookback": "5d",
            "window": 24,
            "as_of": "latest",
        }
    ]
    assert watch["intent"] == "OPEN_LONG"
    assert watch["order"] == {
        "intent": "OPEN_LONG",
        "action": "BUY",
        "qty": 2.0,
        "confidence": 0.72,
        "exit_plan": {"hard_stop": {"type": "price", "price": 155.0}},
        "rationale": "Executer ce plan si le reclaim arrive.",
    }


def test_symbole_sans_decision_est_omis():
    store = _FakeStore([_row("AAPL", "BUY")])

    out = recent_decisions_by_symbol(store, symbols=["AAPL", "MSFT"])

    assert "AAPL" in out
    assert "MSFT" not in out  # pas de clé vide


def test_ne_fuit_pas_les_blobs_et_tronque_le_rationale():
    store = _FakeStore([_row("AAPL", "BUY", rationale="x" * 500)])

    r = recent_decisions_by_symbol(store, symbols=["AAPL"])["AAPL"][0]

    assert "decision" not in r and "market_snapshot" not in r
    assert len(r["rationale"]) <= 201 and r["rationale"].endswith("…")


def test_ne_pousse_pas_les_flags_acceptes_dans_le_feedback_agent():
    warning = {
        "code": "risk_per_trade_exceeded",
        "field": "max_risk_per_trade_pct",
        "risk_pct": 0.015,
        "limit": 0.01,
        "context": "risk_qty=300 max_qty=200 risk_pct=0.015 limit=0.01",
    }
    store = _FakeStore([
        _row(
            "AAPL",
            "BUY",
            runtime={
                "risk_warnings": [warning],
                "tool_calls": [
                    {
                        "tool": "strategy_entry",
                        "outcome": "executed",
                        "detail": {"warnings": [warning]},
                    }
                ],
            },
        )
    ])

    r = recent_decisions_by_symbol(store, symbols=["AAPL"])["AAPL"][0]

    assert "flags" not in r


def test_compacte_les_flags_de_refus_pour_feedback_agent():
    store = _FakeStore([
        _row(
            "AAPL",
            "BUY",
            executed=False,
            reason="risk:order_value_exceeded",
            context="order_value=12000 max_order_value=10000",
            runtime={},
        )
    ])

    r = recent_decisions_by_symbol(store, symbols=["AAPL"])["AAPL"][0]

    assert r["context"] == "order_value=12000 max_order_value=10000"
    assert r["flags"] == [
        {
            "tool": "strategy_entry",
            "outcome": "blocked",
            "code": "order_value_exceeded",
            "context": "order_value=12000 max_order_value=10000",
        }
    ]


def test_compacte_flip_et_scale_in_comme_strategy_entry_sans_trace_outil():
    for intent in ("FLIP", "SCALE_IN"):
        store = _FakeStore([
            _row(
                "AAPL",
                "BUY",
                intent=intent,
                executed=False,
                reason="risk:gross_exposure_exceeded",
                runtime={},
            )
        ])

        r = recent_decisions_by_symbol(store, symbols=["AAPL"])["AAPL"][0]

        assert r["flags"][0]["tool"] == "strategy_entry"
        assert r["flags"][0]["code"] == "gross_exposure_exceeded"


def test_limit_zero_ou_negatif_ne_retourne_rien():
    # Review P2 : rows[-0:] == tout ; un limit <= 0 doit rendre {} (rien demandé).
    store = _FakeStore([_row("AAPL", "BUY"), _row("AAPL", "SELL")])
    assert recent_decisions_by_symbol(store, symbols=["AAPL"], limit=0) == {}
    assert recent_decisions_by_symbol(store, symbols=["AAPL"], limit=-1) == {}
