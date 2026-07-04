"""trader.infrastructure.queue — file de tâches durable in-process.

Découple production et traitement des tâches : les producteurs enfilent sans
bloquer ; un Worker consomme au rythme des ressources disponibles.
Le daemon peut router les décisions et les exécutions par cette file durable via
les flags de queue.

Spec : docs/superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md

Composants
----------
- ``TaskLedger`` (ledger.py) : persistance SQLite + claim atomique.
- ``ResourcePools`` (pools.py) : bornage de concurrence par ressource externe
  avec backpressure AIMD (divise par 2 sur overload, incrémente sur succès).
- ``Worker`` (worker.py) : boucle claim → acquire → handler → complete/fail →
  release ; expose ``RetryableError`` pour le requeue avec backoff exponentiel.

Invariants clés
---------------
- Idempotence : ``dedup_key`` → INSERT OR IGNORE (``enqueue`` retourne None sur
  conflit).
- Sérialisation par ``partition_key`` : au plus 1 tâche running/clé (index
  UNIQUE SQLite), + unicité pending+running par (kind, partition_key).
- Claim resource-aware : ne claim que des tâches dont la ressource est libre
  selon ``ResourcePools.free_resources()``.
- Fencing token : ``claim_token`` vérifié par ``complete``/``fail``/``heartbeat``
  — une opération sur un token révoqué est sans effet.
- Retry + backoff : ``fail(retryable=True)`` → replanifie avec
  ``backoff_base_ms * 2^(attempts-1)`` ; épuisement → status ``'dead'``.
- Reprise au boot : ``recover_on_boot`` remet en ``'pending'`` les tâches
  ``'running'`` dont le bail a expiré.
- Unclaim neutre : ``release_claim`` ne consomme pas de tentative (resource
  miss, etc.) — pas de pénalité sur la tâche.
- Temps injecté ``now_ms`` (epoch ms, int) → déterminisme testable ; stdlib only.

Usage minimal
-------------
::

    from trader.infrastructure.queue.ledger import TaskLedger
    from trader.infrastructure.queue.pools import ResourcePools
    from trader.infrastructure.queue.worker import Worker
    import time

    ledger = TaskLedger("tasks.db")
    ledger.recover_on_boot(now_ms=int(time.time() * 1000))
    ledger.enqueue(kind="send_order", priority=0,
                   scheduled_at_ms=int(time.time() * 1000),
                   now_ms=int(time.time() * 1000),
                   dedup_key="order-42", resource="ib")

    pools  = ResourcePools({"ib": 2})
    worker = Worker(ledger, pools, {"send_order": my_handler}, worker_id="w1")
    ran    = worker.run_once(now_ms=int(time.time() * 1000), token="tok-1")

Conventions AX
--------------
- AX-6 Deterministic Outputs : ``now_ms`` toujours injecté, jamais ``time.time()``
  en interne.
- AX-8 Composable Primitives : Ledger, Pools et Worker sont des classes
  indépendantes, combinables librement.
- AX-9 Narrow Contracts : chaque méthode prend et retourne le minimum utile.
"""
