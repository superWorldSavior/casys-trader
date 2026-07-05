"""Tests TDD — execute_order_unit (unit_of_work.py).

Couvre :
  - Happy path : fill + plan (upsert ou close) + task done — les 3 visibles après.
  - Fill atomique (FIX 1 phase3-lot-b) : task done ET task.result=Fill JSON dans la
    même tx — pas de fenêtre done-sans-result (crash-safe).
  - Atomicité : exception dans upsert_in_tx → ROLLBACK total (aucune écriture).
  - Fence early (FIX 1) : task already done ou token mismatch → RuntimeError,
    aucune écriture.
  - Précondition StateDb (FIX 2) : dbs différents → RuntimeError immédiat,
    aucune écriture.
  - dry_run (FIX 3) : aucune écriture broker NI plan, task complétée.
  - REVERSE = close + upsert (FIX 4) : les deux dans la même UoW, atomicité
    garantie (exception sur upsert rollback aussi le close).
  - Idempotence : complete_in_tx sur tâche déjà 'done' = sans effet (fencing).
  - Rétro-compat : submit/upsert/close_symbol/complete publics inchangés.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trader.planning.trade_plan import TradePlan
from trader.queue.ledger import TaskLedger
from trader.state_db.broker_store import SqliteBroker
from trader.state_db.connection import StateDb
from trader.state_db.migrations import import_broker_from_json, import_trade_plans_from_json
from trader.state_db.trade_plan_store import SqliteTradePlanStore
from trader.state_db.unit_of_work import execute_order_unit
from trader.execution.broker import Order


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
        """execute_order_unit (upsert) écrit fill + plan + task done + task.result."""
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

        # Task done + result contient le Fill JSON (FIX 1 : fill atomique)
        row = db.query_one("SELECT status, result FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "done"
        assert row["result"] is not None, (
            "task.result doit contenir le Fill JSON (écrit dans la même tx que done)"
        )
        import json
        fill_data = json.loads(row["result"])
        assert fill_data["symbol"] == "AAPL"
        assert fill_data["quantity"] == pytest.approx(10.0)

    def test_fill_atomique_status_and_result_in_same_tx(self, tmp_path: Path) -> None:
        """FIX 1 — Invariant atomicité fill : task.status='done' ET task.result=Fill JSON
        dans la MÊME transaction. Pas de fenêtre où la task est done mais result=NULL.

        Ce test vérifie que ledger.get(task_id) retourne les deux en même temps
        après execute_order_unit — sans aucune opération intermédiaire.
        """
        import json as _json
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="atomic-fill")

        execute_order_unit(
            db=db,
            broker=broker,
            plan_store=plan_store,
            ledger=ledger,
            order=Order("AAPL", "BUY", 5.0),
            price=200.0,
            ts="2026-07-04T09:00:00+00:00",
            fx_rate=1.0,
            dry_run=False,
            task_id=task_id,
            token=token,
            now_ms=10,
        )

        # Après execute_order_unit : status ET result sont tous les deux présents
        task_dict = ledger.get(task_id)
        assert task_dict is not None
        assert task_dict["status"] == "done", "status doit être 'done'"
        assert task_dict["result"] is not None, (
            "result doit être non-NULL — fill écrit atomiquement dans la même tx"
        )
        fill_data = _json.loads(task_dict["result"])
        assert fill_data["symbol"] == "AAPL"
        assert fill_data["quantity"] == pytest.approx(5.0)
        assert fill_data["price"] == pytest.approx(200.0)

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
        """dry_run=True : aucune écriture dans broker NI plan, task quand même complétée.

        FIX 3 : en dry_run, ni le broker ni le plan ne sont mutés.
        La tâche est quand même complétée pour éviter le re-play.
        """
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
        # Plan PAS upsert (FIX 3 : dry_run protège aussi les mutations plan)
        assert plan_store.open_plans() == []
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
# Classe 3 — Fence early (FIX 1) : guard AVANT le submit
# ---------------------------------------------------------------------------


class TestFencing:
    """FIX 1 : fence SELECT dans la même transaction, avant submit.

    Task already done ou token mismatch → RuntimeError + ROLLBACK total
    (aucune écriture broker ni plan).
    """

    def test_task_already_done_raises_and_no_writes(self, tmp_path: Path) -> None:
        """Task déjà 'done' → RuntimeError levée, aucune écriture (fill, cash, plan)."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="fence-done")
        cash_before = broker.cash()

        # Compléter manuellement la tâche (simuler un rejeu)
        ok = ledger.complete(task_id=task_id, token=token, now_ms=1)
        assert ok is True
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "done"

        # Rejouer execute_order_unit → doit lever (fence early)
        with pytest.raises(RuntimeError, match="status='done'"):
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

        # Aucune écriture
        assert db.query_all("SELECT * FROM broker_fills") == []
        assert broker.cash() == pytest.approx(cash_before)
        assert plan_store.open_plans() == []

    def test_stale_token_raises_and_no_writes(self, tmp_path: Path) -> None:
        """Token invalide (stale/wrong) → RuntimeError levée, aucune écriture."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="fence-token")
        cash_before = broker.cash()

        with pytest.raises(RuntimeError, match="token mismatch"):
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
                token="wrong-token",  # mauvais token
                now_ms=2,
            )

        # Aucune écriture (ROLLBACK)
        assert db.query_all("SELECT * FROM broker_fills") == []
        assert broker.cash() == pytest.approx(cash_before)
        assert plan_store.open_plans() == []
        # Task toujours running
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "running"


# ---------------------------------------------------------------------------
# Classe 4 — Précondition StateDb (FIX 2)
# ---------------------------------------------------------------------------


class TestPrecondition:
    """FIX 2 : db partagé non-identique → RuntimeError immédiat, aucune écriture."""

    def test_different_broker_db_raises_immediately(self, tmp_path: Path) -> None:
        """broker._db != db → RuntimeError avant ouverture de la transaction."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="pre-broker")
        cash_before = broker.cash()

        # Créer un broker sur une DB différente
        db2 = _make_db(tmp_path, name="other.db")
        broker2 = SqliteBroker(db2)

        with pytest.raises(RuntimeError, match="broker._db is not db"):
            execute_order_unit(
                db=db,
                broker=broker2,  # mauvais db
                plan_store=plan_store,
                ledger=ledger,
                order=Order("AAPL", "BUY", 10.0),
                price=150.0,
                ts="t",
                fx_rate=1.0,
                dry_run=False,
                task_id=task_id,
                token=token,
                now_ms=2,
            )

        # Aucune écriture
        assert db.query_all("SELECT * FROM broker_fills") == []
        assert broker.cash() == pytest.approx(cash_before)

    def test_different_plan_store_db_raises_immediately(self, tmp_path: Path) -> None:
        """plan_store._db != db → RuntimeError avant ouverture de la transaction."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="pre-plan")

        db2 = _make_db(tmp_path, name="other2.db")
        plan_store2 = SqliteTradePlanStore(db2)

        with pytest.raises(RuntimeError, match="plan_store._db is not db"):
            execute_order_unit(
                db=db,
                broker=broker,
                plan_store=plan_store2,  # mauvais db
                ledger=ledger,
                order=Order("AAPL", "BUY", 10.0),
                price=150.0,
                ts="t",
                fx_rate=1.0,
                dry_run=False,
                task_id=task_id,
                token=token,
                now_ms=2,
            )

    def test_different_ledger_db_raises_immediately(self, tmp_path: Path) -> None:
        """ledger._db != db → RuntimeError avant ouverture de la transaction."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        task_id, token = _enqueue_and_claim(ledger, dedup="pre-ledger")

        db2 = _make_db(tmp_path, name="other3.db")
        ledger2 = TaskLedger(db2)
        # Enfile + claime une tâche sur db2 pour avoir un task_id valide
        assert ledger2.enqueue(kind="x", priority=1, scheduled_at_ms=0, now_ms=0, dedup_key="pre-l2") is not None
        task2 = ledger2.claim(worker_id="w", token="tok-l2", now_ms=1, lease_ms=60_000, free_resources=[])

        with pytest.raises(RuntimeError, match="ledger._db is not db"):
            execute_order_unit(
                db=db,
                broker=broker,
                plan_store=plan_store,
                ledger=ledger2,  # mauvais db
                order=Order("AAPL", "BUY", 10.0),
                price=150.0,
                ts="t",
                fx_rate=1.0,
                dry_run=False,
                task_id=task2["id"],
                token=task2["claim_token"],
                now_ms=2,
            )


# ---------------------------------------------------------------------------
# Classe 5 — REVERSE = close + upsert dans la même UoW (FIX 4)
# ---------------------------------------------------------------------------


class TestReverse:
    """FIX 4 : symbol_to_close ET plan_to_upsert non exclusifs.

    REVERSE ferme l'ancien plan PUIS ouvre le nouveau dans la même transaction.
    Exception sur l'upsert rollback aussi le close.
    """

    def test_reverse_close_and_upsert_atomically(self, tmp_path: Path) -> None:
        """REVERSE : ancien plan fermé ET nouveau plan créé dans la même UoW."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)

        # Pré-insère un plan LONG AAPL (position courante)
        plan_store.upsert(_simple_plan("AAPL-long", symbol="AAPL"))
        assert len(plan_store.open_plans()) == 1

        task_id, token = _enqueue_and_claim(ledger, dedup="reverse-1")

        # Nouveau plan SHORT (après REVERSE)
        new_plan = TradePlan(
            id="AAPL-short-new",
            symbol="AAPL",
            side="SHORT",
            quantity=5.0,
            remaining_quantity=5.0,
            entry_price=155.0,
            opened_at="2026-07-04T09:00:00+00:00",
            reference_volatility=0.02,
            hard_stop_price=165.0,
            take_profits=[],
            trailing_stop=None,
            max_hold_minutes=120.0,
            high_watermark=155.0,
            low_watermark=153.0,
            filled_take_profits=[],
            profit_protection=None,
            exit_watch=None,
            llm_provider="openai",
            llm_model="gpt-4o",
            llm_fallback_reason=None,
            llm_confidence=0.80,
            last_llm_review=None,
            entry_thesis="REVERSE thesis",
            entry_decision_id="dec-rev-001",
            entry_context=None,
        )

        # SELL order : ferme la position LONG + ouvre SHORT
        fill = execute_order_unit(
            db=db,
            broker=broker,
            plan_store=plan_store,
            ledger=ledger,
            order=Order("AAPL", "SELL", 15.0),  # close 10 + open 5
            price=155.0,
            ts="t-rev",
            fx_rate=1.0,
            dry_run=False,
            symbol_to_close="AAPL",      # ferme l'ancien plan
            plan_to_upsert=new_plan,     # ouvre le nouveau plan
            task_id=task_id,
            token=token,
            now_ms=3,
        )

        # Fill écrit
        assert fill is not None

        # Ancien plan fermé, nouveau plan présent
        plans = plan_store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "AAPL-short-new"
        assert plans[0].side == "SHORT"

        # Task done
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "done"

    def test_reverse_exception_on_upsert_rolls_back_close(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Exception sur upsert rollback aussi le close : atomicité REVERSE garantie."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)

        # Pré-insère un plan LONG AAPL
        plan_store.upsert(_simple_plan("AAPL-long-2", symbol="AAPL"))
        assert len(plan_store.open_plans()) == 1

        task_id, token = _enqueue_and_claim(ledger, dedup="reverse-rollback")
        cash_before = broker.cash()

        def _failing_upsert_in_tx(cur, plan):
            raise RuntimeError("injected upsert fail on REVERSE")

        monkeypatch.setattr(plan_store, "upsert_in_tx", _failing_upsert_in_tx)

        with pytest.raises(RuntimeError, match="injected upsert fail on REVERSE"):
            execute_order_unit(
                db=db,
                broker=broker,
                plan_store=plan_store,
                ledger=ledger,
                order=Order("AAPL", "SELL", 10.0),
                price=155.0,
                ts="t-rv-rb",
                fx_rate=1.0,
                dry_run=False,
                symbol_to_close="AAPL",
                plan_to_upsert=_simple_plan("AAPL-short-fail"),
                task_id=task_id,
                token=token,
                now_ms=3,
            )

        # ROLLBACK total : ancien plan toujours présent (close rollbacké aussi)
        plans = plan_store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "AAPL-long-2"

        # Cash inchangé (broker rollbacké)
        assert broker.cash() == pytest.approx(cash_before)
        assert db.query_all("SELECT * FROM broker_fills") == []

        # Task toujours running
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task_id,))
        assert row["status"] == "running"


# ---------------------------------------------------------------------------
# Classe 6 — Idempotence (fencing token)
# ---------------------------------------------------------------------------


class TestIdempotence:
    def test_complete_in_tx_noop_on_already_done(self, tmp_path: Path) -> None:
        """complete_in_tx sur tâche déjà 'done' = sans effet (fencing)."""
        db = _make_db(tmp_path)
        ledger = TaskLedger(db)

        assert ledger.enqueue(
            kind="execute", priority=1, scheduled_at_ms=0, now_ms=0, dedup_key="idem-1"
        ) is not None
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

        assert ledger.enqueue(
            kind="execute", priority=1, scheduled_at_ms=0, now_ms=0, dedup_key="idem-2"
        ) is not None
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
# Classe 7 — Rétro-compat des méthodes publiques
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
        assert ledger.enqueue(
            kind="x", priority=1, scheduled_at_ms=0, now_ms=0, dedup_key="rc-1"
        ) is not None
        task = ledger.claim(
            worker_id="w", token="tok-rc", now_ms=1, lease_ms=60_000, free_resources=[]
        )
        ok = ledger.complete(task_id=task["id"], token=task["claim_token"], now_ms=2)
        assert ok is True
        row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
        assert row["status"] == "done"
