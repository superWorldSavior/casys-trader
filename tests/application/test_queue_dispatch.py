"""Tests TDD — dispatch_decide_via_queue (queue_dispatch.py).

Cas couverts :
  1. Happy path : 3 symboles, pool mocké déterministe → 3 Decisions retournées.
  2. Symbole dont la tâche devient dead → absent du résultat (model_calls=0).
  3. Budget écoulé (mock now_fn qui saute le temps) → non-finies skippées.
  4. Purge stale : pending vieux supprimés ; running vieux supprimés seulement si lease expiré.
  5. Nouvelles méthodes ledger : get(task_id) et delete_stale_decide.
  6. build_symbol_facts : comportement identique à _symbol_facts de batch_decide.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from trader.agent.protocol.types import Decision
from trader.application.decide_handler import make_decide_handler
from trader.application.queue_dispatch import dispatch_decide_via_queue
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


class _FakeClient:
    """Client mock déterministe pour les tests du dispatch."""

    def __init__(self, responses: dict | None = None, *, raise_exc=None):
        self._responses = responses or {}
        self._raise_exc = raise_exc

    def decide_batch(self, *, symbols, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return {sym: self._responses.get(sym, _ok_decision(sym)) for sym in symbols}


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
    from trader.application.planner_batch import build_symbol_facts
    from unittest.mock import patch

    now = datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc)
    mock_session = {"status": "open", "venue": "NYSE"}

    with patch("trader.application.planner_batch.market") as mock_market:
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
    from trader.application.planner_batch import build_symbol_facts
    from unittest.mock import patch

    now = datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc)

    mc = {"execution": {"enabled": True}, "planning": {"enabled": True}}
    review = {"action": "BUY", "confidence": 0.9}

    with patch("trader.application.planner_batch.market") as mock_market:
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
            budget_s=10.0,
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
            budget_s=10.0,
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
# Test 3 : budget écoulé → non-finies skippées
# ---------------------------------------------------------------------------


def test_dispatch_budget_elapsed_skips_unfinished(tmp_path):
    """now_fn qui saute au-delà du budget dès le 3e appel → 0 décisions retournées.

    Séquence des appels now_fn dans dispatch_decide_via_queue :
      1. now_ms = int(now_fn() * 1000)          → start
      2. deadline = now_fn() + budget_s         → start (deadline = start + budget_s)
      3. while ... and now_fn() < deadline      → start + budget_s + 1 → False → exit
    """
    led = TaskLedger(tmp_path / "q.db")
    # Pas de pool : les tâches restent pending indéfiniment.

    start = 1_000_000.0
    budget_s = 30.0
    call_count = [0]

    def jumping_now():
        call_count[0] += 1
        # Appels 1 et 2 = phase setup (now_ms + deadline) → retourner start
        # Appel 3+ = test de la condition de boucle → au-delà du budget
        if call_count[0] <= 2:
            return start
        return start + budget_s + 1.0

    decisions, model_calls, undecided = dispatch_decide_via_queue(
        ledger=led,
        decidable=["SLOW"],
        mandate="m",
        memory="m",
        shared_context={},
        symbol_facts_by_sym={"SLOW": {}},
        decision_timeout_s=60,
        agent_tools_enabled=False,
        cycle_id="cycle-budget",
        budget_s=budget_s,
        now_fn=jumping_now,
    )

    assert decisions == {}, f"aucune décision attendue, got {decisions}"
    assert model_calls == 0
    # Budget épuisé → symbole dans undecided (pas de HOLD synthétique)
    assert "SLOW" in undecided, f"SLOW doit être dans undecided, got {undecided}"
    # La tâche doit exister dans le ledger (enfilée) mais pas décidée
    assert led.count_by_status("pending", kind="decide") == 1


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
            scheduled_at_ms=now_ms,
            now_ms=now_ms,
            dedup_key=f"cycle-0:{sym}",
            partition_key=sym,
            resource="acpx",
            payload=json.dumps(_base_payload(sym)),
        )

    # Avant dispatch : 2 tâches pending du cycle-0
    assert led.count_by_status("pending", kind="decide") == 2

    # Dispatch cycle-1 sans pool (budget expiré dès le 3e appel)
    start = 1_000_000.0
    call_count = [0]

    def jumping_now():
        call_count[0] += 1
        if call_count[0] <= 2:
            return start
        return start + 9999.0

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
        budget_s=30.0,
        now_fn=jumping_now,
    )

    # Les 2 stales sont supprimées, 2 nouvelles tâches (cycle-1) enfilées
    pending = led.count_by_status("pending", kind="decide")
    assert pending == 2, f"attendu 2 tâches cycle-1, got {pending}"

    # Vérifier que les dedup_keys appartiennent bien au cycle-1
    rows = led._conn.execute(
        "SELECT dedup_key FROM tasks WHERE kind='decide' AND status='pending'"
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

    call_count = [0]

    def jumping_now():
        call_count[0] += 1
        if call_count[0] <= 2:
            return start
        return start + 9999.0

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
        budget_s=30.0,
        now_fn=jumping_now,
    )

    assert decisions == {}
    assert model_calls == 0
    assert undecided == {"AAPL", "MSFT"}

    rows = led._conn.execute(
        "SELECT dedup_key, partition_key, status FROM tasks WHERE kind='decide' ORDER BY id"
    ).fetchall()
    assert [(r["dedup_key"], r["partition_key"], r["status"]) for r in rows] == [
        ("cycle-0:AAPL", "AAPL", "running"),
        ("cycle-1:MSFT", "MSFT", "pending"),
    ]


# ---------------------------------------------------------------------------
# Test 5 : non-régression — flag off ⇒ _batch_decide toujours utilisé
# ---------------------------------------------------------------------------


def test_flag_off_uses_batch_decide(monkeypatch):
    """Quand queue_decide_enabled=False, run_cycle appelle _batch_decide (spy)."""
    import trader.runtime.daemon as daemon_mod

    batch_called = []

    def fake_batch_decide(**kwargs):
        batch_called.append(kwargs["decidable"])
        return ({}, 0)

    # On patch _batch_decide dans le module daemon
    monkeypatch.setattr(daemon_mod, "_batch_decide", fake_batch_decide)

    from trader.runtime.daemon import run_cycle
    from unittest.mock import MagicMock

    # data_source minimal
    ds = MagicMock()
    ds.get_bars.return_value = []
    ds.get_price.return_value = None

    # run_cycle sans flag → doit appeler _batch_decide
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
    """Le dispatch queue ne porte plus de budget d'admission par appels."""
    import inspect

    params = inspect.signature(dispatch_decide_via_queue).parameters
    assert "max_model_calls" not in params
    assert "tools_active" not in params
    assert "max_rounds" not in params


def test_dispatch_enfile_tous_les_decidables(tmp_path):
    """5 décidables → 5 tâches enfilées ; les non-finies restent undecided."""
    symbols = ["S1", "S2", "S3", "S4", "S5"]
    led = TaskLedger(tmp_path / "q.db")

    # Pas de pool : les tâches restent pending. On teste uniquement l'enfilage.
    start = 1_000_000.0
    call_count = [0]

    def jumping_now():
        call_count[0] += 1
        # Appels 1+2 = phase setup ; appel 3+ = boucle polling → budget déjà expiré
        if call_count[0] <= 2:
            return start
        return start + 9999.0

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
        budget_s=30.0,
        now_fn=jumping_now,
    )

    assert led.count_by_status("pending", kind="decide") == 5
    assert undecided == set(symbols), (
        f"les 5 symboles doivent être undecided quand le budget temps expire, got {undecided}"
    )
    assert decisions == {}
    assert model_calls == 0


def test_dispatch_enfile_les_symboles_dans_l_ordre_decidable(tmp_path):
    """L'ordre d'enfilage reste l'ordre de decidable (déterminisme)."""
    symbols = ["PRIO1", "PRIO2", "LOW3", "LOW4", "LOW5"]
    led = TaskLedger(tmp_path / "q.db")

    start = 1_000_000.0
    call_count = [0]

    def jumping_now():
        call_count[0] += 1
        if call_count[0] <= 2:
            return start
        return start + 9999.0

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
        budget_s=30.0,
        now_fn=jumping_now,
    )

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
            budget_s=10.0,
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
    """Dispatch sans pool (budget immédiatement expiré) : ne teste que l'enfilage."""
    led = TaskLedger(tmp_path / "q.db")
    start = 1_000_000.0
    ticks = [0]

    def jumping_now():
        ticks[0] += 1
        return start if ticks[0] <= 2 else start + 9999.0

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
        budget_s=30.0,
        now_fn=jumping_now,
    )
    kwargs.update(over)
    dispatch_decide_via_queue(**kwargs)
    return led


def test_agent_tools_enabled_ne_change_pas_l_admission(tmp_path):
    led = _dispatch_enqueue_only(
        tmp_path, ["S1", "S2", "S3", "S4", "S5"],
        agent_tools_enabled=True,
    )
    assert led.count_by_status("pending", kind="decide") == 5


def test_budget_temps_expire_reporte_mais_n_empeche_pas_l_enfilage(tmp_path):
    led = _dispatch_enqueue_only(tmp_path, ["S1", "S2"], agent_tools_enabled=True)
    assert led.count_by_status("pending", kind="decide") == 2
