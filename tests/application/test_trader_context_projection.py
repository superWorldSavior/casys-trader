from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from trader.agent.client import build_batch_prompt
from trader.domain.llm import LlmExecutionCapability
from trader.application.decide.context_projection import (
    MAX_FOCUS_ROWS,
    MAX_RADAR_ROWS,
    project_shared_context_for_symbol,
    project_symbol_facts_for_prompt,
)


def _cockpit(symbol_count: int = 48) -> dict:
    cols = [
        "s",
        "f",
        "p",
        "r",
        "vol",
        "z",
        "er",
        "ac",
        "rs",
        "sz",
        "r_d",
        "vol_d",
        "z_d",
        "er_d",
        "ac_d",
        "rs_d",
        "sz_d",
        "reg",
        "vs",
        "st",
        "cndle",
        "htf",
        "aligned",
        "sig",
        "sl24",
        "sh24",
        "sl48",
        "sh48",
        "be_ref_bps",
        "fee",
        "fee_ccy",
        "ccy",
        "fx_usd",
        "risk_budget_native",
        "max_order_native",
    ]
    rows = []
    for index in range(symbol_count):
        symbol = "TARGET" if index == 0 else f"S{index:02d}"
        family = "focus" if index < 14 else f"family-{index % 6}"
        rows.append(
            [
                symbol,
                family,
                100.0 + index,
                index / 100,
                0.1,
                index / 4,
                0.2,
                0.3,
                index / 200,
                index / 5,
                index / 150,
                0.2,
                index / 6,
                0.2,
                0.3,
                index / 250,
                index / 7,
                "trend",
                "normal",
                False,
                None,
                "1d",
                index % 2 == 0,
                ["breakout"] if index % 5 == 0 else None,
                0.02,
                0.03,
                0.04,
                0.05,
                12.0,
                1.5,
                "USD",
                "USD",
                1.0,
                500.0,
                10_000.0,
            ]
        )
    return {
        "v": "cp3",
        "window": 48,
        "daily_window": 15,
        "schema": "full schema",
        "cols": cols,
        "rows": rows,
        "highlights": {
            "abs_r": [["S20", 2.0], ["S21", 1.9]],
            "abs_z": [["S22", 4.0]],
            "abs_sz": [["S23", 3.0]],
        },
        "fee_ref_notional": 10_000.0,
        "fee_scope": "broker_commission_only",
        "fee_estimate_is_all_in": False,
    }


def _shared_context() -> dict:
    symbols = ["TARGET", *[f"S{index:02d}" for index in range(1, 48)]]
    return {
        "now": "2026-07-10T10:00:00+00:00",
        "portfolio": {
            "holdings": [
                {"symbol": "S30", "quantity": 10, "details": "x" * 80},
                {"symbol": "S31", "quantity": 20, "details": "x" * 80},
            ]
        },
        "risk_capacity": {
            "gross_remaining_usd": 50_000.0,
            "per_symbol": {
                symbol: {
                    "price": 100.0,
                    "max_buy_qty": 10.0,
                    "max_sell_qty": 10.0,
                    "debug": "r" * 120,
                }
                for symbol in symbols
            },
        },
        "active_plans_summary": [
            {"symbol": symbol, "id": f"plan-{symbol}", "kind": "armed", "intent": "long"} for symbol in symbols[:30]
        ],
        "stale_market_data": {
            symbol: {"stale_reason": "too_old", "data_age_minutes": 400 + index}
            for index, symbol in enumerate(symbols[:20])
        },
        "attribution": {
            "summary_grain": "flat_to_flat_position_cycle",
            "mechanism_grain": "exit_leg",
            "n_closed_trades": 40,
            "n_closed_position_cycles": 40,
            "n_exit_legs": 57,
            "realized_pnl": None,
            "commission_quality": {
                "status": "unavailable",
                "counts": {"total": 40, "available": 30, "unavailable": 10},
                "reasons": ["commission_not_modeled"],
            },
            "by_confidence": [{"bucket": "0.7-0.8", "n": 10}],
            "by_exit_reason": [{"reason": "stop", "n": 8}],
            "recent_trips": [
                {"symbol": symbol, "pnl": index, "trace": "a" * 180} for index, symbol in enumerate(symbols[:25])
            ],
        },
        "kpis": {
            "equity": 100_000.0,
            "positions": [{"symbol": symbol, "trace": "p" * 120} for symbol in symbols[:20]],
            "model_performance": [{"model": "gpt", "fills": 10, "symbols": symbols, "trace": "m" * 120}],
        },
        "learnings": {
            "global": [{"note": "global"}],
            "by_symbol": {symbol: [{"note": "l" * 100}] for symbol in symbols[:15]},
            "guardrails": [{"note": "stop"}],
        },
        "cockpit": _cockpit(),
        "regime_families": {"focus": {"dir": "up", "frac": 0.8}},
    }


def test_projection_focalise_le_push_sans_muter_le_snapshot() -> None:
    source = _shared_context()
    before = deepcopy(source)

    projected = project_shared_context_for_symbol(source, symbol="TARGET")

    assert source == before
    assert projected["decision_context_scope"]["symbol"] == "TARGET"
    assert set(projected["risk_capacity"]["per_symbol"]) == {"TARGET"}
    assert projected["active_plans_summary"]["total"] == 30
    assert projected["active_plans_summary"]["target"] == [
        {"symbol": "TARGET", "id": "plan-TARGET", "kind": "armed", "intent": "long"}
    ]
    assert {row["symbol"] for row in projected["attribution"]["recent_trips"]} == {"TARGET"}
    assert projected["attribution"]["summary_grain"] == "flat_to_flat_position_cycle"
    assert projected["attribution"]["mechanism_grain"] == "exit_leg"
    assert projected["attribution"]["n_closed_position_cycles"] == 40
    assert projected["attribution"]["n_exit_legs"] == 57
    assert projected["attribution"]["commission_quality"]["status"] == ("unavailable")
    assert "by_confidence" not in projected["attribution"]
    assert "by_exit_reason" not in projected["attribution"]
    assert set(projected["stale_market_data"]) == {"TARGET"}
    assert "positions" not in projected["kpis"]
    assert projected["kpis"]["positions_count"] == 20
    assert "symbols" not in projected["kpis"]["model_performance"][0]
    assert projected["kpis"]["model_performance"][0]["symbol_count"] == 48
    assert "by_symbol" not in projected["learnings"]
    assert projected["learnings"]["scope"]["full_via_tool"] == "recall_learnings"

    cockpit = projected["cockpit"]
    assert len(cockpit["rows"]) <= MAX_RADAR_ROWS
    assert len(cockpit["focus"]["rows"]) == MAX_FOCUS_ROWS
    assert cockpit["focus"]["rows"][0][0] == "TARGET"
    assert all(row[1] == "focus" for row in cockpit["focus"]["rows"])
    assert cockpit["scope"]["source_universe_rows"] == 48
    assert cockpit["scope"]["details_via_tool"] == "get_indicator_context"
    assert cockpit["fee_ref_notional"] == 10_000.0
    assert cockpit["fee_scope"] == "broker_commission_only"
    assert cockpit["fee_estimate_is_all_in"] is False


def test_projection_reduit_fortement_un_contexte_global_repetitif() -> None:
    source = _shared_context()
    projected = project_shared_context_for_symbol(source, symbol="TARGET")

    source_size = len(json.dumps(source, ensure_ascii=False))
    projected_size = len(json.dumps(projected, ensure_ascii=False))

    assert projected_size < source_size * 0.55


def test_projection_laisse_un_contexte_vide_vide() -> None:
    assert project_shared_context_for_symbol({}, symbol="TARGET") == {}


def test_projection_du_mandat_univers_ne_garde_que_la_famille_cible() -> None:
    facts = {
        "universe_mandate": {
            "symbol_mandate": {
                "symbol": "TARGET",
                "family_context": {"family": "focus"},
            },
            "family_postures": {
                "focus": {"posture": "constructive"},
                "unrelated": {"posture": "defensive", "brief": "x" * 2_000},
            },
        },
        "structure": {"price": 100.0},
    }
    before = deepcopy(facts)

    projected = project_symbol_facts_for_prompt(facts, symbol="TARGET")

    assert facts == before
    assert projected["universe_mandate"]["family_postures"] == {"focus": {"posture": "constructive"}}


def test_prompt_focalise_reste_sous_un_budget_de_60k() -> None:
    root = Path(__file__).resolve().parents[2]
    shared = _shared_context()
    shared["cockpit"] = _cockpit(symbol_count=100)
    focused = project_shared_context_for_symbol(shared, symbol="TARGET")
    facts = {
        "company_intelligence_delta": {"summary": "micro/news " * 180},
        "universe_mandate": {"why_selected": "leader de famille"},
        "structure": {"price": 100.0, "swing_low_24": 95.0},
    }

    prompt = build_batch_prompt(
        mandate=(root / "mandate" / "mandate.md").read_text(encoding="utf-8"),
        memory=(root / "mandate" / "memory.md").read_text(encoding="utf-8"),
        shared_context=focused,
        symbols_payload=[{"symbol": "TARGET", **facts}],
        allow_context_request=False,
        allow_tool_calls=True,
        use_symbol_calls_contract=True,
        max_tool_calls_per_symbol=8,
        max_rounds=None,
        execution_capability=LlmExecutionCapability(caged_native_python=True),
    )

    assert len(prompt) < 60_000
    assert '"decision_context_scope":{"version":"decision_focus_v1"' in prompt
