"""Tests TDD — execute_order_handler.py.

Cas couverts :
  - Payload valide (BUY, dry_run=False) → execute_order_unit appelé, Fill JSON retourné.
  - Payload dry_run=True → retourne None (broker no-op).
  - plan_to_upsert dans le payload → reconstruit en TradePlan, passé à UoW.
  - symbol_to_close dans le payload → passé à UoW, plan fermé atomiquement.
  - Exception dans execute_order_unit → remonte sans être avalée (Worker → dead).
  - task.result patché best-effort par Worker après auto-complete (end-to-end).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trader.application.execute_order_handler import make_execute_order_handler
from trader.planning.trade_plan import TradePlan
from trader.queue.ledger import TaskLedger
from trader.state_db.broker_store import SqliteBroker
from trader.state_db.connection import StateDb
from trader.state_db.migrations import import_broker_from_json, import_trade_plans_from_json
from trader.state_db.trade_plan_store import SqliteTradePlanStore
from trader.tools.execution import Fill, Order


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> StateDb:
    db = StateDb(tmp_path / "casys.db")
    import_broker_from_json(db, tmp_path / "_absent_broker.json", starting_cash=100_000.0)
    import_trade_plans_from_json(db, tmp_path / "_absent_plans.json")
    return db


def _make_stack(tmp_path: Path):
    db = _make_db(tmp_path)
    broker = SqliteBroker(db)
    plan_store = SqliteTradePlanStore(db)
    ledger = TaskLedger(db)
    return db, broker, plan_store, ledger


def _enqueue_and_claim(ledger: TaskLedger, *, dedup: str = "ex-1") -> dict:
    """Enfile + claime une tâche ; retourne la task dict avec id et claim_token."""
    tid = ledger.enqueue(
        kind="execute_order",
        priority=0,
        scheduled_at_ms=0,
        now_ms=0,
        dedup_key=dedup,
        resource="portfolio",
    )
    assert tid is not None
    task = ledger.claim(
        worker_id="w",
        token="tok-" + dedup,
        now_ms=1,
        lease_ms=60_000,
        free_resources=["portfolio"],
    )
    assert task is not None
    return task


def _simple_plan(plan_id: str = "AAPL-p1", symbol: str = "AAPL") -> TradePlan:
    return TradePlan(
        id=plan_id,
        symbol=symbol,
        side="LONG",
        quantity=10.0,
        remaining_quantity=10.0,
        entry_price=150.0,
        opened_at="2026-07-04T08:00:00+00:00",
        reference_volatility=0.02,
        hard_stop_price=140.0,
        take_profits=[],
        trailing_stop=None,
        max_hold_minutes=120.0,
        high_watermark=152.0,
        low_watermark=148.0,
        filled_take_profits=[],
        profit_protection=None,
        exit_watch=None,
        llm_provider="openai",
        llm_model="gpt-4o",
        llm_fallback_reason=None,
        llm_confidence=0.85,
        last_llm_review=None,
        entry_thesis="Test thesis",
        entry_decision_id="dec-001",
        entry_context=None,
    )


def _make_task_payload(
    *,
    symbol: str = "AAPL",
    side: str = "BUY",
    quantity: float = 10.0,
    price: float = 150.0,
    ts: str = "2026-07-04T08:00:00+00:00",
    fx_rate: float = 1.0,
    dry_run: bool = False,
    plan_to_upsert: dict | None = None,
    symbol_to_close: str | None = None,
) -> dict:
    return {
        "order": {"symbol": symbol, "side": side, "quantity": quantity, "rationale": "test"},
        "price": price,
        "ts": ts,
        "fx_rate": fx_rate,
        "dry_run": dry_run,
        "plan_to_upsert": plan_to_upsert,
        "symbol_to_close": symbol_to_close,
    }


# ---------------------------------------------------------------------------
# Classe 1 — Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_buy_returns_fill_json(self, tmp_path: Path) -> None:
        """Payload BUY valide → execute_order_unit appelé, Fill JSON retourné."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task = _enqueue_and_claim(ledger, dedup="h1")
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)

        payload = _make_task_payload(symbol="AAPL", side="BUY", quantity=10.0, price=150.0)
        task["payload"] = json.dumps(payload)

        result = handler(task)

        assert result is not None
        fill_data = json.loads(result)
        assert fill_data["symbol"] == "AAPL"
        assert fill_data["side"] == "BUY"
        assert fill_data["quantity"] == pytest.approx(10.0)
        assert fill_data["price"] == pytest.approx(150.0)

        # Broker muté
        assert broker.cash() < 100_000.0

        # Task done (auto-complete par UoW)
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
        assert row["status"] == "done"

    def test_dry_run_returns_none(self, tmp_path: Path) -> None:
        """dry_run=True → retourne None, cash inchangé, task done."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task = _enqueue_and_claim(ledger, dedup="dry1")
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        cash_before = broker.cash()

        payload = _make_task_payload(dry_run=True)
        task["payload"] = json.dumps(payload)

        result = handler(task)

        assert result is None
        assert broker.cash() == pytest.approx(cash_before)
        # Task done même en dry_run (UoW complete s'exécute toujours)
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
        assert row["status"] == "done"

    def test_plan_to_upsert_in_payload_is_persisted(self, tmp_path: Path) -> None:
        """plan_to_upsert dans le payload → reconstruit et upsert par UoW."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task = _enqueue_and_claim(ledger, dedup="plan1")
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)

        plan = _simple_plan("AAPL-test")
        payload = _make_task_payload(plan_to_upsert=asdict(plan))
        task["payload"] = json.dumps(payload)

        result = handler(task)

        assert result is not None  # fill retourné
        plans = plan_store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "AAPL-test"

    def test_symbol_to_close_is_honoured(self, tmp_path: Path) -> None:
        """symbol_to_close → UoW ferme les plans du symbole atomiquement."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        # Pré-insère un plan AAPL
        plan_store.upsert(_simple_plan("AAPL-old"))
        assert len(plan_store.open_plans()) == 1

        task = _enqueue_and_claim(ledger, dedup="close1")
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)

        payload = _make_task_payload(side="SELL", symbol_to_close="AAPL")
        task["payload"] = json.dumps(payload)

        result = handler(task)

        assert result is not None
        # Plan AAPL fermé atomiquement
        assert plan_store.open_plans() == []
        # Task done
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
        assert row["status"] == "done"


# ---------------------------------------------------------------------------
# Classe 2 — Exception remonte (Worker la catchera → dead)
# ---------------------------------------------------------------------------


class TestException:
    def test_exception_in_execute_order_unit_propagates(self, tmp_path: Path, monkeypatch) -> None:
        """Exception dans execute_order_unit → remonte, task reste 'running'."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task = _enqueue_and_claim(ledger, dedup="exc1")
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)

        def _failing_submit_in_tx(cur, order, price, ts, *, dry_run=False, fx_rate=1.0):
            raise RuntimeError("broker indisponible")

        monkeypatch.setattr(broker, "submit_in_tx", _failing_submit_in_tx)

        payload = _make_task_payload()
        task["payload"] = json.dumps(payload)

        with pytest.raises(RuntimeError, match="broker indisponible"):
            handler(task)

        # Task toujours running (ROLLBACK total — pas de complete)
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
        assert row["status"] == "running"
        # Cash inchangé (rollback)
        assert broker.cash() == pytest.approx(100_000.0)


# ---------------------------------------------------------------------------
# Classe 3 — Worker._patch_result : fill dans task.result après auto-complete
# ---------------------------------------------------------------------------


class TestWorkerPatchResult:
    def test_fill_json_patched_into_task_result_after_auto_complete(self, tmp_path: Path) -> None:
        """Worker._patch_result écrit le Fill JSON dans task.result après no-op complete.

        Simule le chemin complet :
          1. Handler exécute execute_order_unit (UoW → task done, result=NULL).
          2. Worker appelle complete() → no-op (fencing, retourne False).
          3. Worker appelle _patch_result → task.result = fill JSON.
          4. Daemon peut lire le fill via ledger.get(task_id)["result"].
        """
        from trader.queue.worker import _patch_result

        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task = _enqueue_and_claim(ledger, dedup="patch1")
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)

        payload = _make_task_payload()
        task["payload"] = json.dumps(payload)

        # Étape 1 : handler auto-complete (UoW → done, result=NULL)
        fill_json = handler(task)
        assert fill_json is not None
        row = db.query_one("SELECT status, result FROM tasks WHERE id=?", (task["id"],))
        assert row["status"] == "done"
        assert row["result"] is None  # UoW ne passe pas de result

        # Étape 2 : Worker appelle complete() → retourne False (fencing)
        finish_ms = 999
        completed = ledger.complete(task_id=task["id"], token=task["claim_token"], now_ms=finish_ms, result=fill_json)
        assert completed is False  # fencing : task déjà done

        # Étape 3 : Worker appelle _patch_result
        _patch_result(ledger, task["id"], fill_json)

        # Étape 4 : task.result contient maintenant le Fill JSON
        row2 = db.query_one("SELECT result FROM tasks WHERE id=?", (task["id"],))
        assert row2["result"] is not None
        fill_data = json.loads(row2["result"])
        assert fill_data["symbol"] == "AAPL"
        assert fill_data["side"] == "BUY"
