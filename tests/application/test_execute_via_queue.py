"""Tests d'intégration — execute_order via le pool de tâches.

Couvre le chemin complet bout-en-bout :
  - StateDb partagé (casys.db de test) + pool DecidePool avec handler execute_order
  - Un ordre enfilé via execute_ledger → pool worker claime → execute_order_unit → done
  - Fill récupéré via ledger.get(task_id)["result"] (JSON — écrit atomiquement par UoW)
  - Broker + plan + task atomiques : si rollback → aucune des 3 écritures persiste
  - FIX 1 : fill dans task.result atomiquement (pas de _patch_result)
  - FIX 3 : ADD via queue → close + upsert nouveau plan atomiques dans UoW

AX §11 Test-First Invariants : atomicité est l'invariant prioritaire.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from trader.application.execute_order_handler import make_execute_order_handler
from trader.planning.trade_plan import TradePlan
from trader.queue.decide_pool import DecidePool
from trader.queue.ledger import TaskLedger
from trader.queue.pools import ResourcePools
from trader.state_db.broker_store import SqliteBroker
from trader.state_db.connection import StateDb
from trader.state_db.migrations import import_broker_from_json, import_trade_plans_from_json
from trader.state_db.trade_plan_store import SqliteTradePlanStore
from trader.tools.execution import Fill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POLL_BUDGET_S = 5.0
_POLL_SLEEP_S = 0.05


def _make_shared_db(tmp_path: Path) -> StateDb:
    db = StateDb(tmp_path / "casys.db")
    import_broker_from_json(db, tmp_path / "_absent_broker.json", starting_cash=100_000.0)
    import_trade_plans_from_json(db, tmp_path / "_absent_plans.json")
    return db


def _make_stack(tmp_path: Path):
    db = _make_shared_db(tmp_path)
    broker = SqliteBroker(db)
    plan_store = SqliteTradePlanStore(db)
    ledger = TaskLedger(db)
    return db, broker, plan_store, ledger


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


def _make_pool(ledger: TaskLedger, handler) -> DecidePool:
    """Pool à 1 worker, ressource 'portfolio'."""
    return DecidePool(
        ledger=ledger,
        pools=ResourcePools({"portfolio": 1}),
        handlers={"execute_order": handler},
        num_workers=1,
        now_fn=time.time,
    )


def _enqueue_order(
    ledger: TaskLedger,
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
) -> int:
    """Enfile une tâche execute_order et retourne son task_id."""
    payload = json.dumps({
        "order": {"symbol": symbol, "side": side, "quantity": quantity, "rationale": "test"},
        "price": price,
        "ts": ts,
        "fx_rate": fx_rate,
        "dry_run": dry_run,
        "plan_to_upsert": plan_to_upsert,
        "symbol_to_close": symbol_to_close,
    })
    now_ms = int(time.time() * 1000)
    tid = ledger.enqueue(
        kind="execute_order",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        resource="portfolio",
        payload=payload,
    )
    assert tid is not None, "enqueue doit retourner un task_id"
    return tid


def _poll_done(ledger: TaskLedger, task_id: int, budget_s: float = _POLL_BUDGET_S) -> dict | None:
    """Attend que la task soit done/dead et retourne le dict, ou None si timeout."""
    deadline = time.time() + budget_s
    while time.time() < deadline:
        task = ledger.get(task_id)
        if task and task["status"] in {"done", "dead"}:
            return task
        time.sleep(_POLL_SLEEP_S)
    return None


# ---------------------------------------------------------------------------
# Classe 1 — Happy path bout-en-bout
# ---------------------------------------------------------------------------


class TestExecuteViaQueue:
    def test_buy_order_executes_atomically(self, tmp_path: Path) -> None:
        """BUY via pool → broker+task done atomiques, Fill JSON dans task.result."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)
        cash_before = broker.cash()

        pool.start()
        try:
            tid = _enqueue_order(ledger, symbol="AAPL", side="BUY", quantity=10.0, price=150.0)
            task = _poll_done(ledger, tid)

            assert task is not None, "task non résolue dans le budget"
            assert task["status"] == "done"

            # Fill JSON dans task.result (écrit atomiquement par UoW — FIX 1)
            assert task["result"] is not None, "task.result doit contenir le Fill JSON"
            fill_data = json.loads(task["result"])
            assert fill_data["symbol"] == "AAPL"
            assert fill_data["side"] == "BUY"
            assert fill_data["quantity"] == pytest.approx(10.0)
            assert fill_data["price"] == pytest.approx(150.0)

            # Broker muté
            assert broker.cash() < cash_before
        finally:
            pool.stop(timeout_s=2.0)

    def test_buy_with_plan_upsert(self, tmp_path: Path) -> None:
        """BUY + plan_to_upsert → broker + plan + task done atomiques."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)

        pool.start()
        try:
            plan = _simple_plan("AAPL-via-queue")
            tid = _enqueue_order(ledger, plan_to_upsert=asdict(plan))
            task = _poll_done(ledger, tid)

            assert task is not None
            assert task["status"] == "done"

            # Plan visible
            plans = plan_store.open_plans()
            assert len(plans) == 1
            assert plans[0].id == "AAPL-via-queue"
        finally:
            pool.stop(timeout_s=2.0)

    def test_close_with_symbol_to_close(self, tmp_path: Path) -> None:
        """SELL + symbol_to_close → plan fermé atomiquement avec le fill."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        # Pré-insère un plan
        plan_store.upsert(_simple_plan("AAPL-old"))
        assert len(plan_store.open_plans()) == 1

        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)

        pool.start()
        try:
            tid = _enqueue_order(ledger, side="SELL", quantity=10.0, symbol_to_close="AAPL")
            task = _poll_done(ledger, tid)

            assert task is not None
            assert task["status"] == "done"
            # Plan AAPL fermé
            assert plan_store.open_plans() == []
        finally:
            pool.stop(timeout_s=2.0)

    def test_dry_run_no_cash_change(self, tmp_path: Path) -> None:
        """dry_run=True → broker inchangé, task done, result=None."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)
        cash_before = broker.cash()

        pool.start()
        try:
            tid = _enqueue_order(ledger, dry_run=True)
            task = _poll_done(ledger, tid)

            assert task is not None
            assert task["status"] == "done"
            # Cash inchangé
            assert broker.cash() == pytest.approx(cash_before)
            # result = None (dry_run → fill=None)
            assert task["result"] is None
        finally:
            pool.stop(timeout_s=2.0)


# ---------------------------------------------------------------------------
# Classe 2 — Atomicité (rollback sur broker fail)
# ---------------------------------------------------------------------------


class TestAtomicite:
    def test_broker_fail_rolls_back_task(self, tmp_path: Path, monkeypatch) -> None:
        """Exception broker → ROLLBACK : broker inchangé, task → dead (max_attempts=1)."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        cash_before = broker.cash()

        def _failing_submit_in_tx(cur, order, price, ts, *, dry_run=False, fx_rate=1.0):
            raise RuntimeError("broker KO")

        monkeypatch.setattr(broker, "submit_in_tx", _failing_submit_in_tx)

        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)

        pool.start()
        try:
            now_ms = int(time.time() * 1000)
            # max_attempts=1 → immédiatement dead sur échec
            tid = ledger.enqueue(
                kind="execute_order",
                priority=0,
                scheduled_at_ms=now_ms,
                now_ms=now_ms,
                resource="portfolio",
                max_attempts=1,
                payload=json.dumps({
                    "order": {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "rationale": ""},
                    "price": 150.0,
                    "ts": "2026-07-04T08:00:00+00:00",
                    "fx_rate": 1.0,
                    "dry_run": False,
                    "plan_to_upsert": None,
                    "symbol_to_close": None,
                }),
            )
            assert tid is not None

            task = _poll_done(ledger, tid)

            assert task is not None
            assert task["status"] == "dead"
            # Cash inchangé (ROLLBACK)
            assert broker.cash() == pytest.approx(cash_before)
        finally:
            pool.stop(timeout_s=2.0)


# ---------------------------------------------------------------------------
# Classe 2b — FIX 2 : dead/timeout fail-closed — ledger state API
# ---------------------------------------------------------------------------


class TestFailClosed:
    """FIX 2 : le poll loop daemon doit distinguer dead/timeout de done.

    Ces tests vérifient l'état final de la tâche dans le ledger, qui est la
    source de vérité utilisée par le daemon pour décider si executed=True.
    """

    def test_dead_task_has_status_dead_and_no_result(self, tmp_path: Path, monkeypatch) -> None:
        """Task dead (retries épuisés) → status='dead', result=NULL, cash inchangé.

        Invariant FIX 2 : le daemon lit ledger.get(tid)["status"] == 'dead'
        et doit loguer executed=False (ce test vérifie la condition préalable).
        """
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        cash_before = broker.cash()

        def _failing_submit_in_tx(cur, order, price, ts, *, dry_run=False, fx_rate=1.0):
            raise RuntimeError("broker KO — task dead après retries")

        monkeypatch.setattr(broker, "submit_in_tx", _failing_submit_in_tx)

        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)

        pool.start()
        try:
            now_ms = int(time.time() * 1000)
            tid = ledger.enqueue(
                kind="execute_order",
                priority=0,
                scheduled_at_ms=now_ms,
                now_ms=now_ms,
                resource="portfolio",
                max_attempts=1,  # → dead immédiatement
                payload=json.dumps({
                    "order": {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "rationale": ""},
                    "price": 150.0,
                    "ts": "2026-07-04T08:00:00+00:00",
                    "fx_rate": 1.0,
                    "dry_run": False,
                    "plan_to_upsert": None,
                    "symbol_to_close": None,
                }),
            )
            assert tid is not None

            task = _poll_done(ledger, tid)

            assert task is not None
            # Condition préalable : task dead → daemon doit lire "dead" et loguer executed=False
            assert task["status"] == "dead", "task doit être dead (retries épuisés)"
            assert task["result"] is None, "result doit être NULL (pas d'exécution)"
            # Cash inchangé (ROLLBACK)
            assert broker.cash() == pytest.approx(cash_before)
        finally:
            pool.stop(timeout_s=2.0)

    def test_done_task_with_fill_is_readable_for_daemon(self, tmp_path: Path) -> None:
        """Task done → result=Fill JSON lisible via ledger.get (chemin daemon FIX 1+2)."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)

        pool.start()
        try:
            tid = _enqueue_order(ledger, symbol="AAPL", side="BUY", quantity=5.0, price=100.0)
            task = _poll_done(ledger, tid)

            assert task is not None
            assert task["status"] == "done"

            # Le daemon lit le fill via ledger.get(tid)["result"] — doit être non-NULL
            task_from_ledger = ledger.get(tid)
            assert task_from_ledger is not None
            assert task_from_ledger["status"] == "done"
            assert task_from_ledger["result"] is not None, (
                "Fill doit être dans task.result pour que daemon puisse lire executed=True"
            )
            fill_data = json.loads(task_from_ledger["result"])
            assert fill_data["symbol"] == "AAPL"
            assert fill_data["quantity"] == pytest.approx(5.0)
        finally:
            pool.stop(timeout_s=2.0)


# ---------------------------------------------------------------------------
# Classe 3 — flag_off : broker.submit synchrone inchangé (non-régression)
# ---------------------------------------------------------------------------


class TestFlagOff:
    """Vérifie que flag_off ⇒ comportement broker.submit synchrone INCHANGÉ."""

    def test_submit_synchrone_when_flag_off(self, tmp_path: Path) -> None:
        """Sans queue, broker.submit est appelé directement (aucune tâche enfilée)."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)
        from trader.tools.execution import Order

        cash_before = broker.cash()
        order = Order(symbol="AAPL", side="BUY", quantity=5.0)
        fill = broker.submit(order, 100.0, "2026-07-04T08:00:00+00:00", dry_run=False, fx_rate=1.0)

        assert fill is not None
        assert fill.symbol == "AAPL"
        assert broker.cash() < cash_before
        # Aucune tâche dans le ledger (la file n'a PAS été utilisée)
        assert ledger.count_by_status("done") == 0
        assert ledger.count_by_status("pending") == 0


# ---------------------------------------------------------------------------
# Classe 4 — FIX 3 : ADD via queue → close + upsert nouveau plan atomiques
# ---------------------------------------------------------------------------


class TestAddAtomique:
    """FIX 3 : en mode queue, ADD ferme l'ancien plan ET insère le nouveau
    dans la même UoW. Pas de fenêtre crash entre le close et l'upsert."""

    def test_add_closes_old_plan_and_inserts_new_atomically(self, tmp_path: Path) -> None:
        """ADD via queue : ancien plan fermé ET nouveau plan créé atomiquement."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)

        # Pré-insère un plan AAPL long
        old_plan = _simple_plan("AAPL-old-add")
        plan_store.upsert(old_plan)
        assert len(plan_store.open_plans()) == 1

        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)

        # Nouveau plan ADD : close l'ancien ET upsert le nouveau atomiquement
        new_plan = _simple_plan("AAPL-new-add")
        from dataclasses import asdict as _asdict
        pool.start()
        try:
            tid = _enqueue_order(
                ledger,
                side="BUY",
                quantity=5.0,
                symbol_to_close="AAPL",      # ferme l'ancien plan
                plan_to_upsert=_asdict(new_plan),  # ouvre le nouveau plan
            )
            task = _poll_done(ledger, tid)

            assert task is not None, "task non résolue"
            assert task["status"] == "done"

            # Fill JSON dans task.result (FIX 1)
            assert task["result"] is not None, "task.result doit contenir le Fill JSON"

            # Ancien plan fermé, nouveau plan présent — ATOMICITÉ
            plans = plan_store.open_plans()
            assert len(plans) == 1, f"exactement 1 plan attendu, got {len(plans)}"
            assert plans[0].id == "AAPL-new-add", (
                f"nouveau plan attendu 'AAPL-new-add', got {plans[0].id}"
            )
        finally:
            pool.stop(timeout_s=2.0)

    def test_add_uow_exception_rolls_back_close_and_upsert(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """ADD via queue : exception sur upsert → rollback du close aussi (atomicité)."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)

        old_plan = _simple_plan("AAPL-old-rb")
        plan_store.upsert(old_plan)
        assert len(plan_store.open_plans()) == 1

        handler = make_execute_order_handler(db=db, broker=broker, plan_store=plan_store, ledger=ledger)
        pool = _make_pool(ledger, handler)

        from trader.state_db.trade_plan_store import SqliteTradePlanStore
        original_upsert_in_tx = SqliteTradePlanStore.upsert_in_tx

        def _failing_upsert_in_tx(self, cur, plan):
            raise RuntimeError("injected upsert failure")

        monkeypatch.setattr(SqliteTradePlanStore, "upsert_in_tx", _failing_upsert_in_tx)

        from dataclasses import asdict as _asdict
        new_plan = _simple_plan("AAPL-new-rb")
        pool.start()
        try:
            now_ms = int(time.time() * 1000)
            tid = ledger.enqueue(
                kind="execute_order",
                priority=0,
                scheduled_at_ms=now_ms,
                now_ms=now_ms,
                resource="portfolio",
                max_attempts=1,  # dead immédiatement
                payload=json.dumps({
                    "order": {"symbol": "AAPL", "side": "BUY", "quantity": 5.0, "rationale": ""},
                    "price": 150.0,
                    "ts": "2026-07-04T08:00:00+00:00",
                    "fx_rate": 1.0,
                    "dry_run": False,
                    "symbol_to_close": "AAPL",
                    "plan_to_upsert": _asdict(new_plan),
                }),
            )
            assert tid is not None

            task = _poll_done(ledger, tid)

            assert task is not None
            assert task["status"] == "dead"  # upsert a échoué → rollback → dead

            # ROLLBACK : ancien plan toujours présent (close et upsert rollbackés)
            plans = plan_store.open_plans()
            assert len(plans) == 1, f"ancien plan doit toujours exister, got {plans}"
            assert plans[0].id == "AAPL-old-rb"
        finally:
            pool.stop(timeout_s=2.0)


# ---------------------------------------------------------------------------
# Classe 5 — FIX 4 : dedup_key → idempotence d'enqueue execute_order
# ---------------------------------------------------------------------------


class TestDedupKey:
    """FIX 4 : enqueue avec le même dedup_key retourne None (pas de doublon)."""

    def test_same_dedup_key_returns_none_on_second_enqueue(self, tmp_path: Path) -> None:
        """Même dedup_key → le 2e enqueue retourne None (idempotent)."""
        db, broker, plan_store, ledger = _make_stack(tmp_path)

        payload = json.dumps({
            "order": {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "rationale": ""},
            "price": 150.0,
            "ts": "2026-07-04T08:00:00+00:00",
            "fx_rate": 1.0,
            "dry_run": False,
            "plan_to_upsert": None,
            "symbol_to_close": None,
        })
        now_ms = int(time.time() * 1000)
        dedup = "exec:2026-07-04T10:00:00+00:00:AAPL:OPEN_LONG"

        tid1 = ledger.enqueue(
            kind="execute_order",
            priority=0,
            scheduled_at_ms=now_ms,
            now_ms=now_ms,
            resource="portfolio",
            partition_key="portfolio",
            dedup_key=dedup,
            payload=payload,
        )
        assert tid1 is not None, "premier enqueue doit retourner un id"

        # Re-enqueue avec le même dedup_key → idempotent (None)
        tid2 = ledger.enqueue(
            kind="execute_order",
            priority=0,
            scheduled_at_ms=now_ms,
            now_ms=now_ms,
            resource="portfolio",
            partition_key="portfolio",
            dedup_key=dedup,
            payload=payload,
        )
        assert tid2 is None, "dedup_key identique → enqueue doit retourner None (idempotence)"

        # Une seule tâche dans la file
        assert ledger.count_by_status("pending") == 1
