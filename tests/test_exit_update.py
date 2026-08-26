"""Tests TDD — levier L3 exit_update (gestion active d'un plan ouvert).

Couvre :
1. Parsing (agent_protocol/parsing.py) — strategy_exit dans calls
2. apply_exit_update (planning/trade_plan.py) — patch d'un TradePlan ouvert
3. Intégration exit_update service — no-op si pas de plan, patch sinon
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from trader.agent import client as codex_client
from trader.application.exit import exit_update as exit_update_service
from trader.planning.trade_plan import (
    InvalidExitPlanError,
    TradePlan,
    apply_exit_update,
    create_trade_plan,
)
from tests.plan_store_fakes import MemoryTradePlanStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plan(
    symbol: str = "SPY",
    side: str = "LONG",
    entry_price: float = 100.0,
    hard_stop_price: float | None = 95.0,
    quantity: float = 10.0,
    opened_at: str = "2026-07-03T10:00:00+00:00",
) -> TradePlan:
    raw_exit: dict = {}
    if hard_stop_price is not None:
        raw_exit["hard_stop"] = hard_stop_price
    return create_trade_plan(
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        opened_at=opened_at,
        raw_exit_plan=raw_exit or None,
    )


def _store_with_plan(plan: TradePlan) -> MemoryTradePlanStore:
    store = MemoryTradePlanStore()
    store.upsert(plan)
    return store


def _parse_calls(calls_json: list[dict], symbol: str = "SPY") -> codex_client.Decision:
    raw = json.dumps({"decisions": [{"symbol": symbol, "confidence": 0.6, "rationale": "test",
                                      "decision_reason_code": "HOLD", "calls": calls_json}]})
    return codex_client.parse_batch(raw, [symbol], allow_context_request=False)[symbol]


# ===========================================================================
# Section 1 : Parsing
# ===========================================================================

class TestParsingExitUpdate:
    """parsing.py — strategy_exit dans calls compilé vers decision.exit_update."""

    def test_exit_update_hard_stop_prix(self) -> None:
        d = _parse_calls([{"tool": "strategy_exit", "args": {"stop": 97.0}}])
        assert d.action == "HOLD"
        assert d.exit_update == {"hard_stop": 97.0}

    def test_exit_update_protect(self) -> None:
        d = _parse_calls([{
            "tool": "strategy_exit",
            "args": {"protect": {"arm_r": 1.0, "giveback": 0.4, "lock_r": 0.0}},
        }])
        assert d.action == "HOLD"
        assert d.exit_update is not None
        assert d.exit_update["profit_protection"]["arm_at_r"] == 1.0
        assert d.exit_update["profit_protection"]["trigger_on_giveback_pct"] == 0.4
        assert d.exit_update["profit_protection"]["lock_r"] == 0.0

    def test_exit_update_trailing(self) -> None:
        d = _parse_calls([{
            "tool": "strategy_exit",
            "args": {"trail": {"type": "percent", "value": 0.015}},
        }])
        assert d.exit_update is not None
        assert d.exit_update["trailing_stop"]["trail_type"] == "percent"
        assert d.exit_update["trailing_stop"]["trail_value"] == 0.015

    def test_exit_update_tp_en_risk_multiple(self) -> None:
        d = _parse_calls([{
            "tool": "strategy_exit",
            "args": {"tp": [{"r": 2.0, "fraction": 1.0}]},
        }])
        assert d.exit_update is not None
        assert d.exit_update["take_profits"] == [{"type": "risk_multiple", "r": 2.0, "fraction": 1.0}]

    def test_exit_update_exit_watch_et_max_hold(self) -> None:
        d = _parse_calls([{
            "tool": "strategy_exit",
            "args": {
                "exit_watch": {
                    "ttl_minutes": 45,
                    "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                },
                "max_hold_minutes": 90,
            },
        }])

        assert d.exit_update is not None
        assert d.exit_update["max_hold_minutes"] == 90
        assert d.exit_update["exit_watch"]["ttl_minutes"] == 45

    def test_strategy_exit_null_clear_rules_survit_du_parse_a_l_application(self) -> None:
        decision = _parse_calls([{
            "tool": "strategy_exit",
            "args": {"exit_watch": None, "max_hold_minutes": None},
        }])
        assert decision.exit_update == {"exit_watch": None, "max_hold_minutes": None}

        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-07-03T10:00:00+00:00",
            raw_exit_plan={
                "max_hold_minutes": 90,
                "exit_watch": {
                    "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                },
            },
        )
        assert decision.exit_update is not None

        patched = apply_exit_update(plan, decision.exit_update)

        assert patched.exit_watch is None
        assert patched.max_hold_minutes is None

    def test_exit_update_args_vides_pas_de_champ(self) -> None:
        """strategy_exit avec args vides → pas de decision.exit_update (no-op propre)."""
        d = _parse_calls([{"tool": "strategy_exit", "args": {}}])
        assert d.exit_update is None

    def test_exit_update_coexiste_avec_hold_explicite(self) -> None:
        """calls:[strategy_exit] = HOLD + ajustement. L'action reste HOLD."""
        d = _parse_calls([{"tool": "strategy_exit", "args": {"stop": 98.0}}])
        assert d.action == "HOLD"
        assert d.intent == "HOLD"
        assert d.exit_update == {"hard_stop": 98.0}

    def test_strategy_exit_ne_coexiste_pas_avec_strategy_entry(self) -> None:
        """strategy_exit + strategy_entry sur le même symbole est rejeté explicitement."""
        d = _parse_calls([
            {"tool": "strategy_entry", "args": {"direction": "long", "qty": 5}},
            {"tool": "strategy_exit", "args": {"stop": 95.0}},
        ])
        assert d.action == "HOLD"
        assert "strategy_exit_conflicts_with_strategy_entry" in d.rationale

    def test_exit_update_calls_vides_pas_de_update(self) -> None:
        """calls:[] → HOLD explicite, pas de exit_update."""
        d = _parse_calls([])
        assert d.action == "HOLD"
        assert d.exit_update is None

    def test_domain_tools_trace_inclut_strategy_exit(self) -> None:
        d = _parse_calls([{"tool": "strategy_exit", "args": {"stop": 97.0}}])
        assert d.domain_tools is not None
        tools = [c["tool"] for c in d.domain_tools["tool_calls"]]
        assert "strategy_exit" in tools


# ===========================================================================
# Section 2 : apply_exit_update (trade_plan.py)
# ===========================================================================

class TestApplyExitUpdate:
    """apply_exit_update — patch d'un TradePlan ouvert."""

    def test_patch_hard_stop_prix(self) -> None:
        plan = _plan(hard_stop_price=95.0)
        patched = apply_exit_update(plan, {"hard_stop": 97.0})
        assert patched.hard_stop_price == 97.0
        # Les autres champs sont inchangés
        assert patched.symbol == plan.symbol
        assert patched.entry_price == plan.entry_price

    def test_patch_trailing_stop(self) -> None:
        plan = _plan()
        patched = apply_exit_update(plan, {"trailing_stop": {"trail_type": "percent", "trail_value": 0.02}})
        assert patched.trailing_stop is not None
        assert patched.trailing_stop.trail_type == "percent"
        assert patched.trailing_stop.trail_value == 0.02
        # hard_stop non touché
        assert patched.hard_stop_price == plan.hard_stop_price

    def test_patch_profit_protection(self) -> None:
        plan = _plan()
        patched = apply_exit_update(plan, {
            "profit_protection": {"arm_at_r": 1.0, "trigger_on_giveback_pct": 0.35, "lock_r": 0.0},
        })
        assert patched.profit_protection is not None
        assert patched.profit_protection.arm_at_r == 1.0
        assert patched.profit_protection.trigger_on_giveback_pct == 0.35
        assert patched.profit_protection.lock_r == 0.0

    def test_patch_take_profits_avec_prix(self) -> None:
        plan = _plan(hard_stop_price=95.0)
        patched = apply_exit_update(plan, {
            "take_profits": [{"type": "price", "price": 108.0, "fraction": 1.0, "name": "tp_update"}],
        })
        assert len(patched.take_profits) == 1
        assert patched.take_profits[0].price == 108.0
        assert patched.take_profits[0].name == "tp_update"

    def test_patch_take_profits_risk_multiple_utilise_stop_plan(self) -> None:
        """TP en R sans hard_stop dans update → utilise plan.hard_stop_price comme référence."""
        plan = _plan(entry_price=100.0, hard_stop_price=95.0)  # stop_distance = 5.0
        patched = apply_exit_update(plan, {
            "take_profits": [{"type": "risk_multiple", "r": 2.0, "fraction": 1.0}],
        })
        # 100 + 2 * 5 = 110
        assert len(patched.take_profits) == 1
        assert patched.take_profits[0].price == pytest.approx(110.0)

    def test_patch_multiple_champs_simultan(self) -> None:
        plan = _plan(hard_stop_price=95.0)
        patched = apply_exit_update(plan, {
            "hard_stop": 97.0,
            "trailing_stop": {"trail_type": "price", "trail_value": 2.0},
        })
        assert patched.hard_stop_price == 97.0
        assert patched.trailing_stop is not None
        assert patched.trailing_stop.trail_value == 2.0

    def test_patch_exit_watch_et_max_hold_depuis_l_horloge_du_cycle(self) -> None:
        opened_at = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
        amended_at = opened_at + timedelta(minutes=60)
        plan = _plan(opened_at=opened_at.isoformat())

        patched = apply_exit_update(
            plan,
            {
                "max_hold_minutes": 90,
                "exit_watch": {
                    "id": "wrong-id",
                    "symbol": "AAPL",
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "last_triggered_at": "2000-01-01T00:00:00+00:00",
                    "ttl_minutes": 120,
                    "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                },
            },
            now=amended_at,
        )

        assert patched.id == plan.id
        assert patched.symbol == plan.symbol == "SPY"
        assert patched.max_hold_minutes == 90.0
        assert patched.exit_watch is not None
        assert patched.exit_watch["id"].startswith("SPY:")
        assert patched.exit_watch["symbol"] == "SPY"
        assert patched.exit_watch["conditions"][0]["symbol"] == "SPY"
        assert patched.exit_watch["created_at"] == amended_at.isoformat()
        assert patched.exit_watch["expires_at"] == (opened_at + timedelta(minutes=90)).isoformat()
        assert "last_triggered_at" not in patched.exit_watch

    def test_patch_exit_watch_exige_l_horloge_de_decision(self) -> None:
        with pytest.raises(InvalidExitPlanError, match="exit_watch_now_required"):
            apply_exit_update(
                _plan(),
                {
                    "exit_watch": {
                        "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                    }
                },
            )

    def test_patch_max_hold_exige_l_horloge_de_decision(self) -> None:
        with pytest.raises(InvalidExitPlanError, match="max_hold_now_required"):
            apply_exit_update(_plan(), {"max_hold_minutes": 90})

    def test_patch_max_hold_raccourci_borne_la_veille_existante(self) -> None:
        opened_at = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at=opened_at.isoformat(),
            raw_exit_plan={
                "max_hold_minutes": 180,
                "exit_watch": {
                    "ttl_minutes": 180,
                    "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                },
            },
        )

        patched = apply_exit_update(
            plan,
            {"max_hold_minutes": 45},
            now=opened_at + timedelta(minutes=30),
        )

        assert patched.max_hold_minutes == 45.0
        assert patched.exit_watch is not None
        assert patched.exit_watch["expires_at"] == (opened_at + timedelta(minutes=45)).isoformat()

    def test_clear_exit_watch_et_max_hold(self) -> None:
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-07-03T10:00:00+00:00",
            raw_exit_plan={
                "max_hold_minutes": 90,
                "exit_watch": {
                    "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                },
            },
        )

        patched = apply_exit_update(plan, {"exit_watch": None, "max_hold_minutes": None})

        assert patched.exit_watch is None
        assert patched.max_hold_minutes is None

    def test_max_hold_hors_plage_calendrier_est_rejete_a_l_entree(self) -> None:
        with pytest.raises(InvalidExitPlanError, match="max_hold_minutes_out_of_range"):
            create_trade_plan(
                symbol="SPY",
                side="LONG",
                quantity=10.0,
                entry_price=100.0,
                opened_at="2026-07-03T10:00:00+00:00",
                raw_exit_plan={
                    "max_hold_minutes": 1e308,
                    "exit_watch": {
                        "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                    },
                },
            )

    def test_max_hold_hors_plage_calendrier_est_rejete_au_remplacement(self) -> None:
        plan = _plan()

        with pytest.raises(InvalidExitPlanError, match="max_hold_minutes_out_of_range"):
            apply_exit_update(
                plan,
                {
                    "max_hold_minutes": 1e308,
                    "exit_watch": {
                        "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                    },
                },
                now=datetime(2026, 7, 3, 11, 0, tzinfo=timezone.utc),
            )

        assert plan.max_hold_minutes is None
        assert plan.exit_watch is None

    def test_max_hold_deja_echu_requiert_strategy_close_sans_cloture_implicite(self) -> None:
        opened_at = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
        now = opened_at + timedelta(minutes=60)
        plan = _plan(opened_at=opened_at.isoformat())
        update = {"max_hold_minutes": 30}

        with pytest.raises(
            InvalidExitPlanError,
            match="max_hold_deadline_elapsed_use_strategy_close",
        ):
            apply_exit_update(plan, update, now=now)

        store = _store_with_plan(plan)
        validation = exit_update_service.validate_exit_update(
            plan_store=store,
            symbol="SPY",
            exit_update=update,
            bars=None,
            now=now,
        )
        entry: dict = {}
        result = exit_update_service.apply_exit_update_to_open_plan(
            plan_store=store,
            symbol="SPY",
            exit_update=update,
            bars=None,
            now=now,
            entry=entry,
        )

        assert validation.would_apply is False
        assert validation.reason == "resolve_failed:max_hold_deadline_elapsed_use_strategy_close"
        assert result.applied is False
        assert result.reason == "resolve_failed:max_hold_deadline_elapsed_use_strategy_close"
        assert store.open_plans() == [plan]
        assert entry["exit_update_applied"] is False

    def test_exit_watch_invalide_n_efface_pas_la_veille_existante(self) -> None:
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-07-03T10:00:00+00:00",
            raw_exit_plan={
                "exit_watch": {
                    "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
                },
            },
        )

        with pytest.raises(InvalidExitPlanError, match="exit_watch_invalid"):
            apply_exit_update(
                plan,
                {"exit_watch": {"conditions": [{"indicator": "unknown", "op": "<", "value": 0}]}},
                now=datetime(2026, 7, 3, 11, 0, tzinfo=timezone.utc),
            )

        assert plan.exit_watch is not None

    @pytest.mark.parametrize("invalid_max_hold", [0, -1, float("inf"), float("nan")])
    def test_max_hold_invalide_est_rejete(self, invalid_max_hold: float) -> None:
        plan = _plan()

        with pytest.raises(InvalidExitPlanError):
            apply_exit_update(plan, {"max_hold_minutes": invalid_max_hold})

        assert plan.max_hold_minutes is None

    def test_update_vide_retourne_plan_identique(self) -> None:
        """Update dict non-None mais sans champs reconnus → plan inchangé."""
        plan = _plan()
        # On passe un dict avec des clés inconnues → _compact_exit_plan retourne None,
        # donc apply_exit_update ne devrait pas être appelée en pratique. Mais si
        # on l'appelle directement avec un dict vide, le plan revient tel quel.
        patched = apply_exit_update(plan, {})
        # Pas de plantage, retourne le plan original
        assert patched.hard_stop_price == plan.hard_stop_price

    def test_hard_stop_type_invalide_leve_erreur(self) -> None:
        plan = _plan()
        with pytest.raises(InvalidExitPlanError):
            # trailing_stop avec trail_type invalide
            apply_exit_update(plan, {
                "trailing_stop": {"trail_type": "invalid_type", "trail_value": 2.0},
            })

    def test_take_profits_risk_multiple_sans_stop_leve_erreur(self) -> None:
        """TP en R et plan sans hard_stop_price → résolution impossible."""
        plan = _plan(hard_stop_price=None)
        with pytest.raises(InvalidExitPlanError):
            apply_exit_update(plan, {
                "take_profits": [{"type": "risk_multiple", "r": 2.0}],
            })


# ===========================================================================
# Section 3 : Intégration exit_update_service.apply_exit_update_to_open_plan
# ===========================================================================

class TestDaemonApplyExitUpdate:
    """exit_update_service.apply_exit_update_to_open_plan — no-op si pas de plan, patch sinon."""

    def test_noop_si_pas_de_plan(self) -> None:
        store = _store_with_plan(_plan("AAPL"))  # plan pour un AUTRE symbole
        entry: dict = {}

        exit_update_service.apply_exit_update_to_open_plan(
            plan_store=store,
            symbol="SPY",  # pas de plan SPY dans le store
            exit_update={"hard_stop": 97.0},
            bars=None,
            entry=entry,
        )

        assert entry["exit_update_applied"] is False
        assert entry["exit_update_reason"] == "no_open_plan"
        assert entry["exit_update"] == {"hard_stop": 97.0}
        # Le plan AAPL est intact
        assert len(store.open_plans()) == 1
        assert store.open_plans()[0].symbol == "AAPL"

    def test_patch_hard_stop_dans_store(self) -> None:
        plan = _plan("SPY", hard_stop_price=95.0)
        store = _store_with_plan(plan)
        entry: dict = {}

        exit_update_service.apply_exit_update_to_open_plan(
            plan_store=store,
            symbol="SPY",
            exit_update={"hard_stop": 97.5},
            bars=None,
            entry=entry,
        )

        assert entry["exit_update_applied"] is True
        assert entry["exit_update"] == {"hard_stop": 97.5}
        updated = store.open_plans()[0]
        assert updated.hard_stop_price == pytest.approx(97.5)

    def test_patch_protect_dans_store(self) -> None:
        plan = _plan("SPY")
        store = _store_with_plan(plan)
        entry: dict = {}

        exit_update_service.apply_exit_update_to_open_plan(
            plan_store=store,
            symbol="SPY",
            exit_update={"profit_protection": {"arm_at_r": 1.0, "trigger_on_giveback_pct": 0.3}},
            bars=None,
            entry=entry,
        )

        assert entry["exit_update_applied"] is True
        updated = store.open_plans()[0]
        assert updated.profit_protection is not None
        assert updated.profit_protection.arm_at_r == 1.0

    def test_resolve_failed_est_un_noop_trace(self) -> None:
        """Résolution impossible → pas d'erreur propagée, entry tracé."""
        plan = _plan("SPY", hard_stop_price=None)
        store = _store_with_plan(plan)
        entry: dict = {}

        # TP en R sans stop_distance → InvalidExitPlanError
        exit_update_service.apply_exit_update_to_open_plan(
            plan_store=store,
            symbol="SPY",
            exit_update={"take_profits": [{"type": "risk_multiple", "r": 2.0}]},
            bars=None,
            entry=entry,
        )

        assert entry["exit_update_applied"] is False
        assert "resolve_failed" in entry["exit_update_reason"]
        # Le plan n'est pas modifié
        assert store.open_plans()[0].hard_stop_price is None

    def test_patch_stop_structurel_protege_gain_dans_store(self) -> None:
        plan = _plan("BAER.SW", entry_price=70.34, hard_stop_price=72.45, quantity=80.0)
        store = _store_with_plan(plan)
        entry = {"price": 74.04}
        bars = [
            {"ts": "t1", "open": 73.8, "high": 74.2, "low": 72.7, "close": 74.0},
            {"ts": "t2", "open": 74.0, "high": 74.3, "low": 72.8, "close": 74.04},
        ]

        exit_update_service.apply_exit_update_to_open_plan(
            plan_store=store,
            symbol="BAER.SW",
            exit_update={
                "hard_stop": {
                    "type": "structural",
                    "anchor": "swing_low",
                    "window": 2,
                    "buffer_pct": 0.002,
                }
            },
            bars=bars,
            entry=entry,
        )

        assert entry["exit_update_applied"] is True
        assert entry.get("exit_update_reason") is None
        assert store.open_plans()[0].hard_stop_price == pytest.approx(72.7 - 70.34 * 0.002)

    def test_stop_structurel_protecteur_reste_borne_par_prix_courant(self) -> None:
        plan = _plan("BAER.SW", entry_price=70.34, hard_stop_price=72.45, quantity=80.0)
        store = _store_with_plan(plan)
        entry = {"price": 74.04}
        bars = [
            {"ts": "t1", "open": 74.2, "high": 74.5, "low": 74.3, "close": 74.4},
            {"ts": "t2", "open": 74.4, "high": 74.6, "low": 74.35, "close": 74.5},
        ]

        exit_update_service.apply_exit_update_to_open_plan(
            plan_store=store,
            symbol="BAER.SW",
            exit_update={"hard_stop": {"type": "structural", "anchor": "swing_low", "window": 2}},
            bars=bars,
            entry=entry,
        )

        assert entry["exit_update_applied"] is False
        assert entry["exit_update_reason"] == "resolve_failed:hard_stop_structural_wrong_side"
        assert store.open_plans()[0].hard_stop_price == pytest.approx(72.45)
