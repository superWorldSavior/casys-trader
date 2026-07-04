"""Tests TDD — execute_order_unit (unit_of_work.py).

Couvre :
  - Happy path : fill + plan (upsert ou close) + task done — les 3 visibles après.
  - Atomicité : exception dans upsert_in_tx → ROLLBACK total (aucune écriture).
  - Idempotence : complete_in_tx sur tâche déjà 'done' = sans effet (fencing).
  - Rétro-compat : submit/upsert/close_symbol/complete publics inchangés.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trader.planning.trade_plan import TradePlan, TrailingStop
from trader.queue.ledger import TaskLedger
from trader.state_db.broker_store import SqliteBroker
from trader.state_db.connection import StateDb
from trader.state_db.migrations import import_broker_from_json, import_trade_plans_from_json
from trader.state_db.trade_plan_store import SqliteTradePlanStore
from trader.state_db.unit_of_work import execute_order_unit
from trader.tools.execution import Order


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, name: str = "casys.db") -> StateDb:
    db = StateDb(tmp_path / name)
    import_broker_from_json(db, tmp_path / "_absent_broker.json", starting_cash=100_000.0)
    import_trade_plans_from_json(db, tmp_path / "_absent_plans.json")
    return db


def _make_stack(
    tmp_path: Path,
) -> tuple[StateDb, SqliteBroker, SqliteTradePlanStore, TaskLedger]:
    """Crée broker + plan_store + ledger partagés sur UN StateDb."""
    db = _make_db(tmp_path)
    broker = SqliteBroker(db)
    plan_store = SqliteTradePlanStore(db)
    ledger = TaskLedger(db)
    return db, broker, plan_store, ledger


def _enqueue_and_claim(
    ledger: TaskLedger, *, dedup: str = "ex-1", partition: str = "AAPL"
) -> tuple[int, str]:
    """Enfile + claime une tâche ; retourne (task_id, token)."""
    tid = ledger.enqueue(
        kind="execute",
        priority=1,
        scheduled_at_ms=0,
        now_ms=0,
        dedup_key=dedup,
        partition_key=partition,
    )
    assert tid is not None
    task = ledger.claim(
        worker_id="w",
        token="tok-" + dedup,
        now_ms=1,
        lease_ms=60_000,
        free_resources=[],
    )
    assert task is not None
    return task["id"], task["claim_token"]


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


# ---------------------------------------------------------------------------
# Classe 1 — Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_execute_order_with_upsert_writes_all_three(self, tmp_path: Path) -> None:
        """execute_order_unit (upsert) écrit fill + plan + task done."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="ex-1")
        plan = _simple_plan()
        cash_before = broker.cash()

        order = Order("AAPL", "BUY", 10.0)
        fill = execute_order_unit(
            db=db,
            broker=broker,
            plan_store=plan_store,
            ledger=ledger,
            order=order,
            price=150.0,
            ts="2026-07-04T08:00:00+00:00",
            fx_rate=1.0,
            dry_run=False,
            plan_to_upsert=plan,
            task_id=task_id,
            token=token,
            now_ms=2,
        )

        # Fill retourné + visible dans les tables
        assert fill is not None
        assert fill.symbol == "AAPL"
        assert fill.quantity == pytest.approx(10.0)
        fills = db.query_all("SELECT * FROM broker_fills")
        assert len(fills) == 1
        assert fills[0]["symbol"] == "AAPL"

        # Cash muté
        assert broker.cash() < cash_before

        # Plan visible
        plans = plan_store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "AAPL-p1"

        # Task done
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "done"

    def test_execute_order_with_close_symbol(self, tmp_path: Path) -> None:
        """execute_order_unit (close) ferme les plans du symbole + fill + task done."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)

        # Pré-insère un plan AAPL
        plan_store.upsert(_simple_plan("AAPL-p0"))
        assert len(plan_store.open_plans()) == 1

        task_id, token = _enqueue_and_claim(ledger, dedup="ex-close")
        order = Order("AAPL", "SELL", 10.0)

        fill = execute_order_unit(
            db=db,
            broker=broker,
            plan_store=plan_store,
            ledger=ledger,
            order=order,
            price=155.0,
            ts="t2",
            fx_rate=1.0,
            dry_run=False,
            symbol_to_close="AAPL",
            task_id=task_id,
            token=token,
            now_ms=3,
        )

        # Fill écrit
        assert fill is not None
        fills = db.query_all("SELECT * FROM broker_fills")
        assert len(fills) == 1

        # Plan AAPL fermé
        assert plan_store.open_plans() == []

        # Task done
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "done"

    def test_execute_order_dry_run_no_writes(self, tmp_path: Path) -> None:
        """dry_run=True : aucune écriture dans broker/plan, task quand même complétée."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="ex-dry")
        cash_before = broker.cash()

        fill = execute_order_unit(
            db=db,
            broker=broker,
            plan_store=plan_store,
            ledger=ledger,
            order=Order("AAPL", "BUY", 5.0),
            price=150.0,
            ts="t",
            fx_rate=1.0,
            dry_run=True,
            plan_to_upsert=_simple_plan(),
            task_id=task_id,
            token=token,
            now_ms=2,
        )

        # fill = None (dry_run)
        assert fill is None
        # Pas de fill en table
        assert db.query_all("SELECT * FROM broker_fills") == []
        # Cash inchangé
        assert broker.cash() == pytest.approx(cash_before)
        # Plan quand même upsert (l'upsert est hors dry_run)
        assert len(plan_store.open_plans()) == 1
        # Task done (complete_in_tx s'exécute toujours)
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "done"


# ---------------------------------------------------------------------------
# Classe 2 — Atomicité (rollback total sur exception)
# ---------------------------------------------------------------------------


class TestAtomicite:
    def test_exception_in_upsert_rolls_back_everything(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Exception dans upsert_in_tx → ROLLBACK : pas de fill, cash inchangé, task non done."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="ex-atomic")
        cash_before = broker.cash()

        # Injection de l'échec dans upsert_in_tx
        original_upsert_in_tx = plan_store.upsert_in_tx

        def _failing_upsert_in_tx(cur, plan):
            raise RuntimeError("injected plan failure")

        monkeypatch.setattr(plan_store, "upsert_in_tx", _failing_upsert_in_tx)

        with pytest.raises(RuntimeError, match="injected plan failure"):
            execute_order_unit(
                db=db,
                broker=broker,
                plan_store=plan_store,
                ledger=ledger,
                order=Order("AAPL", "BUY", 10.0),
                price=150.0,
                ts="t",
                fx_rate=1.0,
                dry_run=False,
                plan_to_upsert=_simple_plan(),
                task_id=task_id,
                token=token,
                now_ms=2,
            )

        # Aucune écriture persistée
        fills = db.query_all("SELECT * FROM broker_fills")
        assert fills == [], f"fills non vides après rollback : {list(fills)}"
        assert broker.cash() == pytest.approx(cash_before), "cash muté malgré rollback"
        assert plan_store.open_plans() == [], "plan inséré malgré rollback"

        # Task toujours running (complete_in_tx n'a pas été committé)
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "running", (
            f"task status={row['status']} — aurait dû rester 'running'"
        )

    def test_exception_in_broker_rolls_back_plan_and_ledger(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Exception dans submit_in_tx → ROLLBACK : pas de plan, task non done."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="ex-broker-fail")

        def _failing_submit_in_tx(cur, order, price, ts, *, dry_run=False, fx_rate=1.0):
            raise RuntimeError("broker down")

        monkeypatch.setattr(broker, "submit_in_tx", _failing_submit_in_tx)

        with pytest.raises(RuntimeError, match="broker down"):
            execute_order_unit(
                db=db,
                broker=broker,
                plan_store=plan_store,
                ledger=ledger,
                order=Order("AAPL", "BUY", 10.0),
                price=150.0,
                ts="t",
                fx_rate=1.0,
                dry_run=False,
                plan_to_upsert=_simple_plan(),
                task_id=task_id,
                token=token,
                now_ms=2,
            )

        # Plan non inséré (rollback)
        assert plan_store.open_plans() == []
        # Task non done
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "running"


# ---------------------------------------------------------------------------
# Classe 3 — Idempotence (fencing token)
# ---------------------------------------------------------------------------


class TestIdempotence:
    def test_complete_in_tx_noop_on_already_done(self, tmp_path: Path) -> None:
        """complete_in_tx sur tâche déjà 'done' = sans effet (fencing)."""
        db = _make_db(tmp_path)
        ledger = TaskLedger(db)

        tid = ledger.enqueue(
            kind="execute", priority=1, scheduled_at_ms=0, now_ms=0, dedup_key="idem-1"
        )
        task = ledger.claim(
            worker_id="w", token="tok-idem", now_ms=1, lease_ms=60_000, free_resources=[]
        )
        task_id = task["id"]
        token = task["claim_token"]

        # Première complétion
        ok1 = ledger.complete(task_id=task_id, token=token, now_ms=2)
        assert ok1 is True

        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "done"

        # Deuxième complétion — fencing : sans effet
        with db.transaction() as cur:
            ok2 = ledger.complete_in_tx(cur, task_id=task_id, token=token, now_ms=3)
        assert ok2 is False, "complete_in_tx doit retourner False si task déjà done"

        # Statut inchangé
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "done"

    def test_complete_in_tx_noop_wrong_token(self, tmp_path: Path) -> None:
        """complete_in_tx avec mauvais token = sans effet."""
        db = _make_db(tmp_path)
        ledger = TaskLedger(db)

        tid = ledger.enqueue(
            kind="execute", priority=1, scheduled_at_ms=0, now_ms=0, dedup_key="idem-2"
        )
        task = ledger.claim(
            worker_id="w", token="real-tok", now_ms=1, lease_ms=60_000, free_resources=[]
        )
        task_id = task["id"]

        with db.transaction() as cur:
            ok = ledger.complete_in_tx(cur, task_id=task_id, token="wrong-tok", now_ms=2)
        assert ok is False

        # Task toujours running
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "running"


# ---------------------------------------------------------------------------
# Classe 4 — Rétro-compat des méthodes publiques
# ---------------------------------------------------------------------------


class TestRetroCompat:
    """Les méthodes publiques submit/upsert/close_symbol/complete sont inchangées."""

    def test_submit_public_writes_fill_and_returns_fill(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        broker = SqliteBroker(db)
        fill = broker.submit(Order("MSFT", "BUY", 5.0), 200.0, "t1", dry_run=False)
        assert fill is not None
        assert fill.symbol == "MSFT"
        assert fill.quantity == pytest.approx(5.0)
        fills = db.query_all("SELECT * FROM broker_fills")
        assert len(fills) == 1

    def test_submit_public_dry_run_no_write(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        broker = SqliteBroker(db)
        cash_before = broker.cash()
        result = broker.submit(Order("MSFT", "BUY", 5.0), 200.0, "t1", dry_run=True)
        assert result is None
        assert broker.cash() == pytest.approx(cash_before)

    def test_upsert_public_inserts_plan(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        store = SqliteTradePlanStore(db)
        store.upsert(_simple_plan("p1"))
        plans = store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "p1"

    def test_close_symbol_public_removes_plans(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        store = SqliteTradePlanStore(db)
        store.upsert(_simple_plan("p-a", "AAPL"))
        store.upsert(_simple_plan("p-b", "AAPL"))
        assert len(store.open_plans()) == 2
        store.close_symbol("AAPL")
        assert store.open_plans() == []

    def test_complete_public_marks_done(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        ledger = TaskLedger(db)
        tid = ledger.enqueue(
            kind="x", priority=1, scheduled_at_ms=0, now_ms=0, dedup_key="rc-1"
        )
        task = ledger.claim(
            worker_id="w", token="tok-rc", now_ms=1, lease_ms=60_000, free_resources=[]
        )
        ok = ledger.complete(task_id=task["id"], token=task["claim_token"], now_ms=2)
        assert ok is True
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
        assert row["status"] == "done"
