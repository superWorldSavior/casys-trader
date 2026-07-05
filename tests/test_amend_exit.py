"""Tests TDD — levier L3 amend_exit (gestion active d'un plan ouvert).

Couvre :
1. Parsing (agent_protocol/parsing.py) — amend_exit dans calls
2. apply_amend_exit (planning/trade_plan.py) — patch d'un TradePlan ouvert
3. Intégration daemon (_apply_amend_exit) — no-op si pas de plan, patch sinon
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from trader.agent import client as codex_client
from trader.planning.trade_plan import (
    InvalidExitPlanError,
    TradePlan,
    TradePlanStore,
    apply_amend_exit,
    create_trade_plan,
)
from trader.runtime import daemon


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


def _store_with_plan(plan: TradePlan) -> tuple[TradePlanStore, Path]:
    # On passe un chemin qui n'existe PAS encore : TradePlanStore._load() retourne
    # {"plans": []} pour un fichier absent, évitant JSONDecodeError sur fichier vide.
    import os
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    os.unlink(tmp_path)  # supprime le fichier vide
    store = TradePlanStore(tmp_path)
    store.upsert(plan)
    return store, tmp_path


def _parse_calls(calls_json: list[dict], symbol: str = "SPY") -> codex_client.Decision:
    raw = json.dumps({"decisions": [{"symbol": symbol, "confidence": 0.6, "rationale": "test",
                                      "decision_reason_code": "HOLD", "calls": calls_json}]})
    return codex_client.parse_batch(raw, [symbol], allow_context_request=False)[symbol]


# ===========================================================================
# Section 1 : Parsing
# ===========================================================================

class TestParsingAmendExit:
    """parsing.py — amend_exit dans calls compilé vers decision.amend_exit."""

    def test_amend_exit_hard_stop_prix(self) -> None:
        d = _parse_calls([{"tool": "amend_exit", "args": {"stop": 97.0}}])
        assert d.action == "HOLD"
        assert d.amend_exit == {"hard_stop": 97.0}

    def test_amend_exit_protect(self) -> None:
        d = _parse_calls([{
            "tool": "amend_exit",
            "args": {"protect": {"arm_r": 1.0, "giveback": 0.4, "lock_r": 0.0}},
        }])
        assert d.action == "HOLD"
        assert d.amend_exit is not None
        assert d.amend_exit["profit_protection"]["arm_at_r"] == 1.0
        assert d.amend_exit["profit_protection"]["trigger_on_giveback_pct"] == 0.4
        assert d.amend_exit["profit_protection"]["lock_r"] == 0.0

    def test_amend_exit_trailing(self) -> None:
        d = _parse_calls([{
            "tool": "amend_exit",
            "args": {"trail": {"type": "percent", "value": 0.015}},
        }])
        assert d.amend_exit is not None
        assert d.amend_exit["trailing_stop"]["trail_type"] == "percent"
        assert d.amend_exit["trailing_stop"]["trail_value"] == 0.015

    def test_amend_exit_tp_en_risk_multiple(self) -> None:
        d = _parse_calls([{
            "tool": "amend_exit",
            "args": {"tp": [{"r": 2.0, "fraction": 1.0}]},
        }])
        assert d.amend_exit is not None
        assert d.amend_exit["take_profits"] == [{"type": "risk_multiple", "r": 2.0, "fraction": 1.0}]

    def test_amend_exit_args_vides_pas_de_champ(self) -> None:
        """amend_exit avec args vides → pas de decision.amend_exit (no-op propre)."""
        d = _parse_calls([{"tool": "amend_exit", "args": {}}])
        assert d.amend_exit is None

    def test_amend_exit_coexiste_avec_hold_explicite(self) -> None:
        """calls:[amend_exit] = HOLD + ajustement. L'action reste HOLD."""
        d = _parse_calls([{"tool": "amend_exit", "args": {"stop": 98.0}}])
        assert d.action == "HOLD"
        assert d.intent == "HOLD"
        assert d.amend_exit == {"hard_stop": 98.0}

    def test_amend_exit_ne_coexiste_pas_avec_propose_order(self) -> None:
        """amend_exit + propose_order sur le même symbole est rejeté explicitement."""
        d = _parse_calls([
            {"tool": "propose_order", "args": {"intent": "OPEN_LONG", "qty": 5}},
            {"tool": "amend_exit", "args": {"stop": 95.0}},
        ])
        assert d.action == "HOLD"
        assert "amend_exit_conflicts_with_propose_order" in d.rationale

    def test_amend_exit_calls_vides_pas_de_amend(self) -> None:
        """calls:[] → HOLD explicite, pas de amend_exit."""
        d = _parse_calls([])
        assert d.action == "HOLD"
        assert d.amend_exit is None

    def test_domain_tools_trace_inclut_amend_exit(self) -> None:
        d = _parse_calls([{"tool": "amend_exit", "args": {"stop": 97.0}}])
        assert d.domain_tools is not None
        tools = [c["tool"] for c in d.domain_tools["tool_calls"]]
        assert "amend_exit" in tools


# ===========================================================================
# Section 2 : apply_amend_exit (trade_plan.py)
# ===========================================================================

class TestApplyAmendExit:
    """apply_amend_exit — patch d'un TradePlan ouvert."""

    def test_patch_hard_stop_prix(self) -> None:
        plan = _plan(hard_stop_price=95.0)
        patched = apply_amend_exit(plan, {"hard_stop": 97.0})
        assert patched.hard_stop_price == 97.0
        # Les autres champs sont inchangés
        assert patched.symbol == plan.symbol
        assert patched.entry_price == plan.entry_price

    def test_patch_trailing_stop(self) -> None:
        plan = _plan()
        patched = apply_amend_exit(plan, {"trailing_stop": {"trail_type": "percent", "trail_value": 0.02}})
        assert patched.trailing_stop is not None
        assert patched.trailing_stop.trail_type == "percent"
        assert patched.trailing_stop.trail_value == 0.02
        # hard_stop non touché
        assert patched.hard_stop_price == plan.hard_stop_price

    def test_patch_profit_protection(self) -> None:
        plan = _plan()
        patched = apply_amend_exit(plan, {
            "profit_protection": {"arm_at_r": 1.0, "trigger_on_giveback_pct": 0.35, "lock_r": 0.0},
        })
        assert patched.profit_protection is not None
        assert patched.profit_protection.arm_at_r == 1.0
        assert patched.profit_protection.trigger_on_giveback_pct == 0.35
        assert patched.profit_protection.lock_r == 0.0

    def test_patch_take_profits_avec_prix(self) -> None:
        plan = _plan(hard_stop_price=95.0)
        patched = apply_amend_exit(plan, {
            "take_profits": [{"type": "price", "price": 108.0, "fraction": 1.0, "name": "tp_amend"}],
        })
        assert len(patched.take_profits) == 1
        assert patched.take_profits[0].price == 108.0
        assert patched.take_profits[0].name == "tp_amend"

    def test_patch_take_profits_risk_multiple_utilise_stop_plan(self) -> None:
        """TP en R sans hard_stop dans amend → utilise plan.hard_stop_price comme référence."""
        plan = _plan(entry_price=100.0, hard_stop_price=95.0)  # stop_distance = 5.0
        patched = apply_amend_exit(plan, {
            "take_profits": [{"type": "risk_multiple", "r": 2.0, "fraction": 1.0}],
        })
        # 100 + 2 * 5 = 110
        assert len(patched.take_profits) == 1
        assert patched.take_profits[0].price == pytest.approx(110.0)

    def test_patch_multiple_champs_simultan(self) -> None:
        plan = _plan(hard_stop_price=95.0)
        patched = apply_amend_exit(plan, {
            "hard_stop": 97.0,
            "trailing_stop": {"trail_type": "price", "trail_value": 2.0},
        })
        assert patched.hard_stop_price == 97.0
        assert patched.trailing_stop is not None
        assert patched.trailing_stop.trail_value == 2.0

    def test_amend_vide_retourne_plan_identique(self) -> None:
        """Amend dict non-None mais sans champs reconnus → plan inchangé."""
        plan = _plan()
        # On passe un dict avec des clés inconnues → _compact_exit_plan retourne None,
        # donc apply_amend_exit ne devrait pas être appelée en pratique. Mais si
        # on l'appelle directement avec un dict vide, le plan revient tel quel.
        patched = apply_amend_exit(plan, {})
        # Pas de plantage, retourne le plan original
        assert patched.hard_stop_price == plan.hard_stop_price

    def test_hard_stop_type_invalide_leve_erreur(self) -> None:
        plan = _plan()
        with pytest.raises(InvalidExitPlanError):
            # trailing_stop avec trail_type invalide
            apply_amend_exit(plan, {
                "trailing_stop": {"trail_type": "invalid_type", "trail_value": 2.0},
            })

    def test_take_profits_risk_multiple_sans_stop_leve_erreur(self) -> None:
        """TP en R et plan sans hard_stop_price → résolution impossible."""
        plan = _plan(hard_stop_price=None)
        with pytest.raises(InvalidExitPlanError):
            apply_amend_exit(plan, {
                "take_profits": [{"type": "risk_multiple", "r": 2.0}],
            })


# ===========================================================================
# Section 3 : Intégration daemon._apply_amend_exit
# ===========================================================================

class TestDaemonApplyAmendExit:
    """daemon._apply_amend_exit — no-op si pas de plan, patch sinon."""

    def test_noop_si_pas_de_plan(self) -> None:
        _, tmp_path = _store_with_plan(_plan("AAPL"))  # plan pour un AUTRE symbole
        store = TradePlanStore(tmp_path)
        entry: dict = {}

        daemon._apply_amend_exit(
            plan_store=store,
            symbol="SPY",  # pas de plan SPY dans le store
            amend_exit={"hard_stop": 97.0},
            bars=None,
            entry=entry,
        )

        assert entry["amend_exit_applied"] is False
        assert entry["amend_exit_reason"] == "no_open_plan"
        assert entry["amend_exit"] == {"hard_stop": 97.0}
        # Le plan AAPL est intact
        assert len(store.open_plans()) == 1
        assert store.open_plans()[0].symbol == "AAPL"

    def test_patch_hard_stop_dans_store(self) -> None:
        plan = _plan("SPY", hard_stop_price=95.0)
        store, _ = _store_with_plan(plan)
        entry: dict = {}

        daemon._apply_amend_exit(
            plan_store=store,
            symbol="SPY",
            amend_exit={"hard_stop": 97.5},
            bars=None,
            entry=entry,
        )

        assert entry["amend_exit_applied"] is True
        assert entry["amend_exit"] == {"hard_stop": 97.5}
        updated = store.open_plans()[0]
        assert updated.hard_stop_price == pytest.approx(97.5)

    def test_patch_protect_dans_store(self) -> None:
        plan = _plan("SPY")
        store, _ = _store_with_plan(plan)
        entry: dict = {}

        daemon._apply_amend_exit(
            plan_store=store,
            symbol="SPY",
            amend_exit={"profit_protection": {"arm_at_r": 1.0, "trigger_on_giveback_pct": 0.3}},
            bars=None,
            entry=entry,
        )

        assert entry["amend_exit_applied"] is True
        updated = store.open_plans()[0]
        assert updated.profit_protection is not None
        assert updated.profit_protection.arm_at_r == 1.0

    def test_resolve_failed_est_un_noop_trace(self) -> None:
        """Résolution impossible → pas d'erreur propagée, entry tracé."""
        plan = _plan("SPY", hard_stop_price=None)
        store, _ = _store_with_plan(plan)
        entry: dict = {}

        # TP en R sans stop_distance → InvalidExitPlanError
        daemon._apply_amend_exit(
            plan_store=store,
            symbol="SPY",
            amend_exit={"take_profits": [{"type": "risk_multiple", "r": 2.0}]},
            bars=None,
            entry=entry,
        )

        assert entry["amend_exit_applied"] is False
        assert "resolve_failed" in entry["amend_exit_reason"]
        # Le plan n'est pas modifié
        assert store.open_plans()[0].hard_stop_price is None
