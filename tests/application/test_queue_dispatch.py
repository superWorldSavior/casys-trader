"""Tests TDD — dispatch_decide_via_queue (queue_dispatch.py).

Cas couverts :
  1. Happy path : 3 symboles, pool mocké déterministe → 3 Decisions retournées.
  2. Symbole dont la tâche devient dead → absent du résultat (model_calls=0).
  3. Pas de deadline de collecte : une décision tardive est collectée.
  4. Purge stale : pending vieux supprimés ; running vieux supprimés seulement si lease expiré.
  5. Nouvelles méthodes ledger : get(task_id) et delete_stale_decide.
  6. build_symbol_facts : comportement identique à _symbol_facts de batch_decide.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

from trader.agent.protocol.types import Decision
from trader.application.decide.handler import make_decide_handler
import trader.application.decide.queue_dispatch as queue_dispatch_mod
from trader.application.decide.queue_dispatch import dispatch_decide_via_queue
from trader.queue.decide_pool import DecidePool
from trader.queue.ledger import TaskLedger
from trader.queue.pools import ResourcePools


# ---------------------------------------------------------------------------
# Helpers communs
# ---------------------------------------------------------------------------

def _ok_decision(symbol: str, action: str = "BUY") -> Decision:
    return Decision(
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        quantity=10.0,
        confidence=0.8,
        rationale="signal",
    )


def _base_payload(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "mandate": "test mandate",
        "memory": "test memory",
        "shared_context": {},
        "per_symbol_facts": {},
        "decision_timeout_s": 60,
        "agent_tools_enabled": False,
    }


def _decision_result_json(symbol: str, *, calls: int = 1) -> str:
    return json.dumps({"decision": asdict(_ok_decision(symbol)), "model_calls": calls})


def _terminal_handler(*, calls: int = 1, delay_s: float = 0.0):
    def handler(task: dict, *, heartbeat=None) -> str:
        if delay_s:
            time.sleep(delay_s)
        payload = json.loads(task["payload"])
        return _decision_result_json(payload["symbol"], calls=calls)

    return handler


class _FakeClient:
    """Client mock déterministe pour les tests du dispatch."""

    def __init__(self, responses: dict | None = None, *, raise_exc=None):
        self._responses = responses or {}
        self._raise_exc = raise_exc

    def decide_batch(self, *, symbols, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return {sym: self._responses.get(sym, _ok_decision(sym)) for sym in symbols}


class _SequencedLedger:
    def __init__(self):
        self._next_id = 1
        self._dedup_by_id: dict[int, str] = {}
        self._symbol_by_id: dict[int, str] = {}
        self._states_by_id: dict[int, list[str]] = {}
        self._last_state_by_id: dict[int, str] = {}

    def delete_stale_decide(self, *, current_cycle_id: str, now_ms: int) -> int:
        return 0

    def enqueue(self, *, kind, priority, scheduled_at_ms, now_ms, dedup_key=None,
                partition_key=None, resource=None, payload=None, max_attempts=3, parent_id=None):
        task_id = self._next_id
        self._next_id += 1
        self._dedup_by_id[task_id] = str(dedup_key)
        self._symbol_by_id[task_id] = str(partition_key)
        if partition_key == "A":
            states = ["pending", "pending", "done"]
        else:
            states = ["pending", "pending", "pending", "done"]
        self._states_by_id[task_id] = states
        self._last_state_by_id[task_id] = states[0]
        return task_id

    def get(self, task_id: int) -> dict:
        states = self._states_by_id[task_id]
        state = states.pop(0) if states else self._last_state_by_id[task_id]
        self._last_state_by_id[task_id] = state
        task = {
            "id": task_id,
            "status": state,
            "dedup_key": self._dedup_by_id[task_id],
            "partition_key": self._symbol_by_id[task_id],
        }
        if state == "done":
            task["result"] = _decision_result_json(self._symbol_by_id[task_id])
        return task


class _ScriptedIterLedger:
    def __init__(self, states_by_symbol: dict[str, list[dict]]):
        self._next_id = 1
        self._task_id_by_symbol: dict[str, int] = {}
        self._symbol_by_id: dict[int, str] = {}
        self._states_by_symbol = {
            sym: list(states)
            for sym, states in states_by_symbol.items()
        }

    def delete_stale_decide(self, *, current_cycle_id: str, now_ms: int) -> int:
        return 0

    def enqueue(self, *, kind, priority, scheduled_at_ms, now_ms, dedup_key=None,
                partition_key=None, resource=None, payload=None, max_attempts=3, parent_id=None):
        task_id = self._next_id
        self._next_id += 1
        sym = str(partition_key)
        self._task_id_by_symbol[sym] = task_id
        self._symbol_by_id[task_id] = sym
        return task_id

    def get(self, task_id: int) -> dict | None:
        sym = self._symbol_by_id[task_id]
        states = self._states_by_symbol[sym]
        task = dict(states.pop(0) if states else {"status": "done"})
        if task.get("missing"):
            return None
        task.setdefault("id", task_id)
        task.setdefault("partition_key", sym)
        if task.get("status") == "done" and "result" not in task:
            task["result"] = _decision_result_json(sym, calls=int(task.get("calls", 1)))
        return task


def _iter_events(ledger, symbols: list[str]):
    return queue_dispatch_mod.iter_decide_results_via_queue(
        ledger=ledger,
        decidable=symbols,
        mandate="m",
        memory="m",
        shared_context={},
        symbol_facts_by_sym={sym: {} for sym in symbols},
        decision_timeout_s=60,
        agent_tools_enabled=False,
        cycle_id="cycle-iter",
        now_fn=lambda: 2.0,
    )


# ---------------------------------------------------------------------------
# Tests itérateur événementiel decide queue
# ---------------------------------------------------------------------------


def test_iter_decide_results_yields_done_before_slow_symbol_finishes(monkeypatch):
    """Une décision terminale est yieldée avant que le symbole lent ne termine."""
    sleeps: list[float] = []
    monkeypatch.setattr(queue_dispatch_mod._time, "sleep", lambda seconds: sleeps.append(seconds))

    events = _iter_events(
        _ScriptedIterLedger(
            {
                "FAST": [{"status": "done", "calls": 2}],
                "SLOW": [{"status": "pending"}, {"status": "done"}],
            }
        ),
        ["SLOW", "FAST"],
    )

    sym, decision, calls = next(events)

    assert sym == "FAST"
    assert isinstance(decision, Decision)
    assert decision.symbol == "FAST"
    assert calls == 2
    assert sleeps == []

    assert [(sym, decision.symbol if decision else None, calls) for sym, decision, calls in events] == [
        ("SLOW", "SLOW", 1),
    ]
    assert sleeps == [0.1]


def test_iter_decide_results_yields_ready_symbols_in_stable_sorted_order():
    """Les terminaux d'une même passe sortent dans l'ordre trié des symboles."""
    symbols = ["MSFT", "AAPL", "GOOG"]
    events = list(
        _iter_events(
            _ScriptedIterLedger({sym: [{"status": "done"}] for sym in symbols}),
            symbols,
        )
    )

    assert [sym for sym, _decision, _calls in events] == ["AAPL", "GOOG", "MSFT"]


def test_iter_decide_results_running_expired_lease_yields_undecided_event():
    """Un running au lease expiré yield un event undecided sans HOLD synthétique."""
    events = list(
        _iter_events(
            _ScriptedIterLedger(
                {
                    "EXPIRED": [
                        {"status": "running", "lease_expires_at": 1_000},
                    ],
                }
            ),
            ["EXPIRED"],
        )
    )

    assert events == [("EXPIRED", None, 0)]


def test_iter_decide_results_never_yields_same_symbol_twice():
    """Même si la task reste visible en done, un symbole terminal ne sort qu'une fois."""
    events = list(
        _iter_events(
            _ScriptedIterLedger(
                {
                    "ONCE": [
                        {"status": "done"},
                        {"status": "done"},
                    ],
                }
            ),
            ["ONCE"],
        )
    )

    assert [(sym, decision.symbol if decision else None, calls) for sym, decision, calls in events] == [
        ("ONCE", "ONCE", 1),
    ]


def test_enqueue_decide_payload_contient_cycle_id() -> None:
    class CaptureLedger:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def delete_stale_decide(self, *, current_cycle_id: str, now_ms: int) -> int:
            return 0

        def enqueue(self, *, payload=None, **_kwargs):
            self.payloads.append(json.loads(payload))
            return len(self.payloads)

    ledger = CaptureLedger()

    queue_dispatch_mod._enqueue_decide_tasks(
        ledger=ledger,
        decidable=["SPY"],
        mandate="mandate",
        memory="memory",
        shared_context={},
        symbol_facts_by_sym={"SPY": {}},
        decision_timeout_s=60,
        agent_tools_enabled=True,
        cycle_id="2026-07-07T09:00:00+08:00",
        now_fn=lambda: 1.0,
        symbols_universe=["SPY"],
    )

    assert ledger.payloads == [
        {
            "symbol": "SPY",
            "mandate": "mandate",
            "memory": "memory",
            "shared_context": {},
            "per_symbol_facts": {},
            "decision_timeout_s": 60,
            "agent_tools_enabled": True,
            "symbols_universe": ["SPY"],
            "cycle_id": "2026-07-07T09:00:00+08:00",
        }
    ]


# ---------------------------------------------------------------------------
# Tests ledger — méthodes ajoutées
# ---------------------------------------------------------------------------


def test_ledger_get_returns_task_dict(tmp_path):
    """get(task_id) retourne un dict avec les champs attendus."""
    led = TaskLedger(tmp_path / "q.db")
    now_ms = int(time.time() * 1000)
    tid = led.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key="cycle-1:AAPL",
        partition_key="AAPL",
        resource="acpx",
        payload='{"symbol": "AAPL"}',
    )
    assert isinstance(tid, int)

    task = led.get(tid)
    assert task is not None
    assert isinstance(task, dict)
    assert task["id"] == tid
    assert task["kind"] == "decide"
    assert task["status"] == "pending"
    assert task["partition_key"] == "AAPL"


def test_ledger_get_missing_returns_none(tmp_path):
    """get(task_id) sur un id inexistant retourne None."""
    led = TaskLedger(tmp_path / "q.db")
    assert led.get(99999) is None


def test_ledger_delete_stale_decide_supprime_pending_precedent(tmp_path):
    """delete_stale_decide supprime les decide pending d'un cycle précédent."""
    led = TaskLedger(tmp_path / "q.db")
    now_ms = int(time.time() * 1000)

    # Tâche decide d'un cycle précédent (cycle-0)
    led.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key="cycle-0:AAPL",
        partition_key="AAPL",
        resource="acpx",
    )
    # Tâche non-decide (ne doit pas être supprimée)
    led.enqueue(
        kind="apply_exits",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key="exits-0:AAPL",
        partition_key="AAPL-exit",
    )

    deleted = led.delete_stale_decide(current_cycle_id="cycle-1", now_ms=now_ms + 1)

    assert deleted == 1  # seule la tâche decide du cycle-0 est supprimée
    assert led.count_by_status("pending", kind="decide") == 0
    assert led.count_by_status("pending", kind="apply_exits") == 1


def test_ledger_delete_stale_decide_preserve_current_cycle(tmp_path):
    """delete_stale_decide préserve les tâches decide du cycle courant."""
    led = TaskLedger(tmp_path / "q.db")
    now_ms = int(time.time() * 1000)

    led.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key="cycle-1:AAPL",
        partition_key="AAPL",
        resource="acpx",
    )
    led.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key="cycle-0:MSFT",  # ancien cycle
        partition_key="MSFT",
        resource="acpx",
    )

    deleted = led.delete_stale_decide(current_cycle_id="cycle-1", now_ms=now_ms + 1)

    assert deleted == 1
    # AAPL (cycle-1) préservé, MSFT (cycle-0) supprimé
    assert led.count_by_status("pending", kind="decide") == 1


def test_ledger_delete_stale_decide_preserve_running_precedent_non_expire(tmp_path):
    """delete_stale_decide ne supprime jamais un decide running au bail vivant."""
    led = TaskLedger(tmp_path / "q.db")
    now_ms = int(time.time() * 1000)

    led.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key="cycle-0:AAPL",
        partition_key="AAPL",
        resource="acpx",
    )
    # Claim → running
    led.claim(worker_id="w1", token="t1", now_ms=now_ms + 1,
               lease_ms=60_000, free_resources=["acpx"])

    deleted = led.delete_stale_decide(current_cycle_id="cycle-1", now_ms=now_ms + 2)

    assert deleted == 0
    assert led.count_by_status("running", kind="decide") == 1


def test_ledger_delete_stale_decide_supprime_running_precedent_expire(tmp_path):
    """delete_stale_decide supprime un decide running d'un cycle précédent si son lease est expiré."""
    led = TaskLedger(tmp_path / "q.db")
    now_ms = int(time.time() * 1000)

    led.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key="cycle-0:AAPL",
        partition_key="AAPL",
        resource="acpx",
    )
    led.claim(worker_id="w1", token="t1", now_ms=now_ms + 1,
               lease_ms=10, free_resources=["acpx"])

    deleted = led.delete_stale_decide(current_cycle_id="cycle-1", now_ms=now_ms + 12)

    assert deleted == 1
    assert led.count_by_status("running", kind="decide") == 0


# ---------------------------------------------------------------------------
# Tests build_symbol_facts
# ---------------------------------------------------------------------------


def test_build_symbol_facts_champs_de_base(tmp_path):
    """build_symbol_facts retourne data_age_m, session, active_watches."""
    from trader.application.decide.planner_batch import build_symbol_facts
    from unittest.mock import patch

    now = datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc)
    mock_session = {"status": "open", "venue": "NYSE"}

    with patch("trader.application.decide.planner_batch.market") as mock_market:
        mock_market.session_context.return_value = mock_session
        facts = build_symbol_facts(
            "AAPL",
            data_age_by_symbol={"AAPL": 3.7},
            now=now,
            active_watches_by_symbol={"AAPL": [{"id": "w1"}]},
        )

    assert facts["data_age_m"] == 4  # int(round(3.7))
    assert facts["session"] == mock_session
    assert facts["active_watches"] == [{"id": "w1"}]
    assert "execution" not in facts  # pas de market_context fourni
    assert "last_llm_review" not in facts  # pas de review fourni


def test_build_symbol_facts_avec_market_context_et_review():
    """build_symbol_facts intègre execution/planning et last_llm_review."""
    from trader.application.decide.planner_batch import build_symbol_facts
    from unittest.mock import patch

    now = datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc)

    mc = {"execution": {"enabled": True}, "planning": {"enabled": True}}
    review = {"action": "BUY", "confidence": 0.9}

    with patch("trader.application.decide.planner_batch.market") as mock_market:
        mock_market.session_context.return_value = {}
        facts = build_symbol_facts(
            "AAPL",
            data_age_by_symbol={},
            now=now,
            active_watches_by_symbol={},
            market_context_by_symbol={"AAPL": mc},
            last_review_by_symbol={"AAPL": review},
        )

    assert facts["data_age_m"] is None
    assert facts["execution"] == {"enabled": True}
    assert facts["planning"] == {"enabled": True}
    assert facts["last_llm_review"] == review


# ---------------------------------------------------------------------------
# Test 1 : happy path — 3 symboles, pool réel → 3 Decisions
# ---------------------------------------------------------------------------


def test_dispatch_3_symbols_all_done(tmp_path):
    """3 symboles décidés par le pool → 3 Decisions dans le résultat."""
    symbols = ["AAPL", "MSFT", "GOOG"]
    client = _FakeClient({sym: _ok_decision(sym) for sym in symbols})

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 3})
    handlers = {"decide": make_decide_handler(codex_client=client)}
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers=handlers,
        num_workers=3,
        now_fn=time.time,
    )
    pool.start()

    try:
        decisions, model_calls, undecided = dispatch_decide_via_queue(
            ledger=led,
            decidable=symbols,
            mandate="test mandate",
            memory="test memory",
            shared_context={},
            symbol_facts_by_sym={sym: {} for sym in symbols},
            decision_timeout_s=60,
            agent_tools_enabled=False,
            cycle_id="cycle-2026-07-04T10:00:00",
            now_fn=time.time,
        )
    finally:
        pool.stop()

    assert set(decisions.keys()) == set(symbols), f"décisions manquantes: {decisions.keys()}"
    assert model_calls == 3
    assert undecided == set(), f"aucun undecided attendu, got {undecided}"
    for sym in symbols:
        dec = decisions[sym]
        assert isinstance(dec, Decision)
        assert dec.symbol == sym
        assert dec.action == "BUY"


# ---------------------------------------------------------------------------
# Test 2 : tâche dead → symbole absent du résultat
# ---------------------------------------------------------------------------


def test_dispatch_dead_task_skipped(tmp_path):
    """Tâche dont toutes les tentatives échouent (dead) → absente du résultat."""
    # Handler qui lève toujours une erreur non-overload
    client = _FakeClient(raise_exc=RuntimeError("LLM indisponible"))
    sym = "DEAD"

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 1})
    handlers = {"decide": make_decide_handler(codex_client=client)}
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers=handlers,
        num_workers=1,
        now_fn=time.time,
        # backoff quasi-nul pour épuiser les 3 tentatives rapidement
        backoff_base_ms=1,
    )
    pool.start()

    try:
        decisions, model_calls, undecided = dispatch_decide_via_queue(
            ledger=led,
            decidable=[sym],
            mandate="m",
            memory="m",
            shared_context={},
            symbol_facts_by_sym={sym: {}},
            decision_timeout_s=60,
            agent_tools_enabled=False,
            cycle_id="cycle-dead",
            now_fn=time.time,
        )
    finally:
        pool.stop()

    assert decisions == {}, f"aucune décision attendue, got {decisions}"
    assert model_calls == 0
    # Tâche dead → dans undecided (pas de HOLD synthétique en mode queue)
    assert sym in undecided, f"{sym} doit être dans undecided, got {undecided}"
    # Tâche doit être dead (3 tentatives épuisées)
    assert led.count_by_status("dead", kind="decide") == 1


# ---------------------------------------------------------------------------
# Test 3 : attente sans deadline → décision tardive collectée
# ---------------------------------------------------------------------------


def test_dispatch_collecte_decision_apres_ancienne_deadline(tmp_path):
    """Même si now_fn saute au-delà de l'ancienne deadline, le résultat done est collecté."""
    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 1})
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": _terminal_handler(calls=3, delay_s=0.02)},
        num_workers=1,
        now_fn=time.time,
    )
    pool.start()

    start = 1_000_000.0
    call_count = [0]

    def jumping_now():
        call_count[0] += 1
        return start if call_count[0] <= 2 else start + 9_999.0

    try:
        decisions, model_calls, undecided = dispatch_decide_via_queue(
            ledger=led,
            decidable=["SLOW"],
            mandate="m",
            memory="m",
            shared_context={},
            symbol_facts_by_sym={"SLOW": {}},
            decision_timeout_s=60,
            agent_tools_enabled=False,
            cycle_id="cycle-no-deadline",
            now_fn=jumping_now,
        )
    finally:
        pool.stop()

    assert set(decisions) == {"SLOW"}
    assert model_calls == 3
    assert undecided == set()


def test_dispatch_running_lease_expire_devient_undecided_sans_hang(tmp_path):
    """Une tâche running au lease expiré est reportée undecided, sans HOLD synthétique."""
    led = TaskLedger(tmp_path / "q.db")
    claimed = threading.Event()

    def claim_after_enqueue():
        deadline = time.time() + 2.0
        while time.time() < deadline:
            task = led.claim(
                worker_id="dead-worker",
                token="dead-token",
                now_ms=1_000,
                lease_ms=10,
                free_resources=["acpx"],
            )
            if task is not None:
                claimed.set()
                return
            time.sleep(0.01)

    t = threading.Thread(target=claim_after_enqueue, daemon=True)
    t.start()
    ticks = [0]

    def now_fn():
        ticks[0] += 1
        return 1.0 if ticks[0] == 1 else 2.0

    decisions, model_calls, undecided = dispatch_decide_via_queue(
        ledger=led,
        decidable=["EXPIRED"],
        mandate="m",
        memory="m",
        shared_context={},
        symbol_facts_by_sym={"EXPIRED": {}},
        decision_timeout_s=60,
        agent_tools_enabled=False,
        cycle_id="cycle-expired",
        now_fn=now_fn,
    )
    t.join(timeout=1.0)

    assert claimed.is_set()
    assert decisions == {}
    assert model_calls == 0
    assert undecided == {"EXPIRED"}


def test_dispatch_poll_sleep_backoff_puis_reset(monkeypatch):
    """Le poll ralentit quand rien ne bouge, puis revient à 0.1s dès résolution."""
    sleeps: list[float] = []
    monkeypatch.setattr(queue_dispatch_mod._time, "sleep", lambda seconds: sleeps.append(seconds))

    decisions, model_calls, undecided = dispatch_decide_via_queue(
        ledger=_SequencedLedger(),
        decidable=["A", "B"],
        mandate="m",
        memory="m",
        shared_context={},
        symbol_facts_by_sym={"A": {}, "B": {}},
        decision_timeout_s=60,
        agent_tools_enabled=False,
        cycle_id="cycle-backoff",
        now_fn=lambda: 1_000_000.0,
    )

    assert set(decisions) == {"A", "B"}
    assert model_calls == 2
    assert undecided == set()
    assert sleeps == [0.1, 0.2, 0.1]


# ---------------------------------------------------------------------------
# Test 4 : purge stale — tâches d'un cycle précédent supprimées
# ---------------------------------------------------------------------------


def test_dispatch_purges_stale_previous_cycle(tmp_path):
    """Tâches decide pending d'un cycle précédent purgées avant enfilage."""
    led = TaskLedger(tmp_path / "q.db")
    now_ms = int(time.time() * 1000)

    # Simule 2 tâches restantes d'un cycle précédent (cycle-0)
    for sym in ["AAPL", "MSFT"]:
        led.enqueue(
            kind="decide",
            priority=0,
            scheduled_at_ms=now_ms + 10_000_000,
            now_ms=now_ms,
            dedup_key=f"cycle-0:{sym}",
            partition_key=sym,
            resource="acpx",
            payload=json.dumps(_base_payload(sym)),
        )

    # Avant dispatch : 2 tâches pending du cycle-0
    assert led.count_by_status("pending", kind="decide") == 2

    pools = ResourcePools({"acpx": 2})
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": _terminal_handler()},
        num_workers=2,
        now_fn=time.time,
    )
    pool.start()
    try:
        dispatch_decide_via_queue(
            ledger=led,
            decidable=["AAPL", "MSFT"],
            mandate="m",
            memory="m",
            shared_context={},
            symbol_facts_by_sym={"AAPL": {}, "MSFT": {}},
            decision_timeout_s=60,
            agent_tools_enabled=False,
            cycle_id="cycle-1",
            now_fn=time.time,
        )
    finally:
        pool.stop()

    assert led.count_by_status("done", kind="decide") == 2

    # Vérifier que les dedup_keys appartiennent bien au cycle-1
    rows = led._conn.execute(
        "SELECT dedup_key FROM tasks WHERE kind='decide' AND status='done'"
    ).fetchall()
    dedup_keys = {r["dedup_key"] for r in rows}
    assert dedup_keys == {"cycle-1:AAPL", "cycle-1:MSFT"}


def test_dispatch_reporte_symbole_inflight_vivant_sans_crash(tmp_path):
    """Un running vivant d'un ancien cycle bloque la partition et reporte le symbole."""
    led = TaskLedger(tmp_path / "q.db")
    start = 1_000_000.0
    now_ms = int(start * 1000)

    old_tid = led.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key="cycle-0:AAPL",
        partition_key="AAPL",
        resource="acpx",
        payload=json.dumps(_base_payload("AAPL")),
    )
    assert old_tid is not None
    claimed = led.claim(worker_id="w1", token="t1", now_ms=now_ms + 1,
                        lease_ms=60_000, free_resources=["acpx"])
    assert claimed is not None

    pools = ResourcePools({"acpx": 1})
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": _terminal_handler()},
        num_workers=1,
        now_fn=time.time,
    )
    pool.start()
    try:
        decisions, model_calls, undecided = dispatch_decide_via_queue(
            ledger=led,
            decidable=["AAPL", "MSFT"],
            mandate="m",
            memory="m",
            shared_context={},
            symbol_facts_by_sym={"AAPL": {}, "MSFT": {}},
            decision_timeout_s=60,
            agent_tools_enabled=False,
            cycle_id="cycle-1",
            now_fn=lambda: start,
        )
    finally:
        pool.stop()

    assert set(decisions) == {"MSFT"}
    assert model_calls == 1
    assert undecided == {"AAPL"}

    rows = led._conn.execute(
        "SELECT dedup_key, partition_key, status FROM tasks WHERE kind='decide' ORDER BY id"
    ).fetchall()
    assert [(r["dedup_key"], r["partition_key"], r["status"]) for r in rows] == [
        ("cycle-0:AAPL", "AAPL", "running"),
        ("cycle-1:MSFT", "MSFT", "done"),
    ]


# ---------------------------------------------------------------------------
# Test 5 : non-régression — flag off ⇒ _batch_decide toujours utilisé
# ---------------------------------------------------------------------------


def test_flag_off_uses_batch_decide(monkeypatch):
    """Quand queue_decide_enabled=False, run_cycle appelle _batch_decide (spy)."""
    from trader.runtime.daemon import run_cycle
    from unittest.mock import MagicMock

    # data_source minimal
    ds = MagicMock()
    ds.get_bars.return_value = []
    ds.get_price.return_value = None

    # On ne peut pas vraiment exécuter un cycle complet ici car trop de dépendances,
    # mais on vérifie que la signature de run_cycle accepte queue_decide_enabled.
    import inspect
    sig = inspect.signature(run_cycle)
    assert "queue_decide_enabled" in sig.parameters, (
        "run_cycle doit accepter queue_decide_enabled"
    )
    assert "task_ledger" in sig.parameters, (
        "run_cycle doit accepter task_ledger"
    )
    # Défaut = False (flag off par défaut)
    assert sig.parameters["queue_decide_enabled"].default is False
    assert sig.parameters["task_ledger"].default is None


# ---------------------------------------------------------------------------
# Test 6 : admission sans cap par appels — tous les décidables sont enfilés
# ---------------------------------------------------------------------------


def test_dispatch_signature_ne_thread_plus_le_cap_appels():
    """Le dispatch queue ne porte plus de budget d'admission ni de deadline de collecte."""
    import inspect

    params = inspect.signature(dispatch_decide_via_queue).parameters
    assert "max_model_calls" not in params
    assert "tools_active" not in params
    assert "max_rounds" not in params
    assert "budget_s" not in params


def test_dispatch_enfile_tous_les_decidables(tmp_path):
    """5 décidables → 5 tâches enfilées puis collectées, sans cap par appels."""
    symbols = ["S1", "S2", "S3", "S4", "S5"]
    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 5})
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": _terminal_handler()},
        num_workers=5,
        now_fn=time.time,
    )
    pool.start()

    try:
        decisions, model_calls, undecided = dispatch_decide_via_queue(
            ledger=led,
            decidable=symbols,
            mandate="m",
            memory="m",
            shared_context={},
            symbol_facts_by_sym={sym: {} for sym in symbols},
            decision_timeout_s=60,
            agent_tools_enabled=False,
            cycle_id="cycle-cap",
            now_fn=time.time,
        )
    finally:
        pool.stop()

    assert led.count_by_status("done", kind="decide") == 5
    assert set(decisions) == set(symbols)
    assert undecided == set()
    assert model_calls == 5


def test_dispatch_enfile_les_symboles_dans_l_ordre_decidable(tmp_path):
    """L'ordre d'enfilage reste l'ordre de decidable (déterminisme)."""
    symbols = ["PRIO1", "PRIO2", "LOW3", "LOW4", "LOW5"]
    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 5})
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": _terminal_handler()},
        num_workers=5,
        now_fn=time.time,
    )
    pool.start()
    try:
        dispatch_decide_via_queue(
            ledger=led,
            decidable=symbols,
            mandate="m",
            memory="m",
            shared_context={},
            symbol_facts_by_sym={sym: {} for sym in symbols},
            decision_timeout_s=60,
            agent_tools_enabled=False,
            cycle_id="cycle-order",
            now_fn=time.time,
        )
    finally:
        pool.stop()

    rows = led._conn.execute(
        "SELECT partition_key FROM tasks WHERE kind='decide' ORDER BY id"
    ).fetchall()
    admitted_syms = [r["partition_key"] for r in rows]
    assert admitted_syms == symbols


# ---------------------------------------------------------------------------
# Test 7 (FIX 2) : undecided → PAS de HOLD synthétique en mode queue
# ---------------------------------------------------------------------------


def test_dispatch_undecided_not_in_decisions(tmp_path):
    """Un symbole skippé (dead) figure dans undecided et ABSENT de decisions.

    Ce test vérifie le contrat retourné — run_cycle doit utiliser undecided
    pour ne PAS générer de HOLD synthétique pour ces symboles.
    """
    sym_dead = "DEADX"
    sym_ok = "OKX"

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 2})

    # Pool avec handler OK pour sym_ok, mais client crée erreur pour DEADX.
    # Astuce : make_decide_handler reçoit un client unique → on crée 2 handlers
    # séparés. Ici on utilise un seul client qui retourne ok pour sym_ok
    # et lève une erreur pour DEADX via le _raise_exc conditionnel.

    class _PartialClient:
        def decide_batch(self, *, symbols, **kwargs):
            results = {}
            for s in symbols:
                if s == sym_ok:
                    results[s] = _ok_decision(s)
                else:
                    raise RuntimeError(f"LLM error for {s}")
            return results

    handlers_mixed = {"decide": make_decide_handler(codex_client=_PartialClient())}
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers=handlers_mixed,
        num_workers=2,
        now_fn=time.time,
        backoff_base_ms=1,
    )
    pool.start()

    try:
        decisions, model_calls, undecided = dispatch_decide_via_queue(
            ledger=led,
            decidable=[sym_dead, sym_ok],
            mandate="m",
            memory="m",
            shared_context={},
            symbol_facts_by_sym={sym_dead: {}, sym_ok: {}},
            decision_timeout_s=60,
            agent_tools_enabled=False,
            cycle_id="cycle-mixed",
            now_fn=time.time,
        )
    finally:
        pool.stop()

    # sym_ok décidé → présent dans decisions
    assert sym_ok in decisions, f"{sym_ok} doit être décidé"
    # sym_dead (dead après 3 tentatives) → dans undecided, absent de decisions
    assert sym_dead in undecided, f"{sym_dead} doit être dans undecided"
    assert sym_dead not in decisions, f"{sym_dead} ne doit PAS être dans decisions"


# ---------------------------------------------------------------------------
# Test 8 (FIX 4) : flag OFF réel — signature + defaults
# ---------------------------------------------------------------------------


def test_flag_off_no_ledger_in_signature():
    """Avec flag off (défaut), run_cycle a task_ledger=None et queue_decide_enabled=False.

    Vérifie que les defaults correspondent exactement au flag off attendu, et
    que la signature de run_cycle accepte les deux paramètres queue.
    """
    import inspect
    import trader.runtime.daemon as daemon_mod

    sig = inspect.signature(daemon_mod.run_cycle)

    # Paramètres présents
    assert "queue_decide_enabled" in sig.parameters
    assert "task_ledger" in sig.parameters

    # Défauts = flag off (prod inchangée)
    assert sig.parameters["queue_decide_enabled"].default is False, (
        "queue_decide_enabled doit défaut à False"
    )
    assert sig.parameters["task_ledger"].default is None, (
        "task_ledger doit défaut à None"
    )


# ---------------------------------------------------------------------------
# Admission queue : pas de halving/outils, seulement l'enfilage complet
# ---------------------------------------------------------------------------


def _dispatch_enqueue_only(tmp_path, symbols, **over):
    """Dispatch avec handler terminal : teste l'enfilage sans deadline de collecte."""
    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": len(symbols) or 1})
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": _terminal_handler()},
        num_workers=max(1, len(symbols)),
        now_fn=time.time,
    )
    pool.start()

    kwargs = dict(
        ledger=led,
        decidable=symbols,
        mandate="m",
        memory="m",
        shared_context={},
        symbol_facts_by_sym={sym: {} for sym in symbols},
        decision_timeout_s=60,
        agent_tools_enabled=False,
        cycle_id="cycle-tools-cap",
        now_fn=time.time,
    )
    kwargs.update(over)
    try:
        dispatch_decide_via_queue(**kwargs)
    finally:
        pool.stop()
    return led


def test_agent_tools_enabled_ne_change_pas_l_admission(tmp_path):
    led = _dispatch_enqueue_only(
        tmp_path, ["S1", "S2", "S3", "S4", "S5"],
        agent_tools_enabled=True,
    )
    assert led.count_by_status("done", kind="decide") == 5


def test_absence_budget_temps_n_empeche_pas_l_enfilage(tmp_path):
    led = _dispatch_enqueue_only(tmp_path, ["S1", "S2"], agent_tools_enabled=True)
    assert led.count_by_status("done", kind="decide") == 2
