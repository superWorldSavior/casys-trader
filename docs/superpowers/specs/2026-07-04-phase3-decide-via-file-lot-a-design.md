# Phase 3 — `decide` via la file durable (Lot A incrémental, grain-symbole)

> Sous-phase du chantier task-ledger. Design d'architecture général :
> `docs/superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md` (§4.1, §4.3bis,
> §4.4, §4.5, §4.6). Ce document précise le **comment** de la bascule `decide → file`
> (Lot A), validé avec Erwan le 2026-07-04.

## 1. Objectif

Basculer l'orchestration du `decide` du daemon (aujourd'hui `_batch_decide` synchrone +
`ThreadPoolExecutor`, `planner_batch.py`) pour qu'elle passe par la **file durable**
(`trader/queue/`). Bénéfices ciblés (Lot A, la file ne passe **aucun ordre**) :

- **Fin du HOLD-par-saturation** : un `decide` qui échoue/traîne est **requeue** (retry +
  backoff), plus jamais un HOLD synthétique définitif.
- **Backpressure adaptative** sur `acpx` (AIMD) : `M` guichets remplacent
  `CASYS_DECISION_BATCH_PARALLELISM=1`.
- **Isolation native par symbole** : `partition_key = symbole` (grain-symbole).
- **Durabilité/reprise** : les `decide` en cours survivent à un crash (recover au boot).

**Non-objectif de cette phase** (Lot B) : l'exécution des ordres reste **synchrone dans
`run_cycle`, inchangée**. Seule l'orchestration du `decide` bascule.

## 2. Décisions validées (2026-07-04)

- **Grain-symbole (K=1)** : `1 tâche decide = 1 symbole = 1 appel acpx`. Remplace le chunk
  batch (~5 symboles/prompt). Conforme §4.3bis du design chantier + demande explicite Erwan
  (« un seul symbole à la fois »).
- **Exécution inchangée** : `order_admission` + `SimBroker.submit` restent synchrones dans
  `run_cycle`, après collecte des décisions. Le gate de pertinence, les `armed_decisions`, le
  recording, le scheduler, la consolidation : **inchangés**.
- **Budget de temps du cycle** = `CASYS_DECISION_TIMEOUT_S` (défaut 900s) : plafond d'attente
  des tâches `decide` d'un cycle. Au-delà, les non-finies restent `pending` (redécidées au
  cycle suivant) et leur symbole est **skippé** ce cycle (pas de HOLD synthétique).
- **Réversibilité (strangler)** : flag `CASYS_QUEUE_DECIDE_ENABLED` (défaut **off** =
  `_batch_decide` actuel intact ; **on** = via file). Rollback = flag, comme le shadow.

## 3. Architecture

```
run_cycle (PRODUCTEUR, synchrone)
  1. calcule `decidable` (gate de pertinence — INCHANGÉ)
  2. si CASYS_QUEUE_DECIDE_ENABLED : enfile 1 tâche `decide` par symbole
       kind=decide, partition_key=sym, resource=acpx,
       dedup_key=f"{cycle_id}:{sym}", payload={sym, contexte par-symbole}
       (sinon : chemin `_batch_decide` actuel, inchangé)
  3. ATTEND ses tâches (cycle_id) jusqu'à done/dead OU budget écoulé
       (polling léger sur le ledger ; réveil sur complétion)
  4. COLLECTE les Decision depuis task.result ; symboles non finis = skippés
  5. fusion armed_decisions + EXÉCUTION synchrone (order_admission + submit) — INCHANGÉ

Pool de N worker-threads (démarré au boot du daemon, vit tant que le daemon vit)
  claim resource-aware (acpx = M guichets, backpressure AIMD §4.5)
  handler `decide_one(payload)` : construit le prompt 1-symbole, appelle acpx,
    parse la Decision, écrit task.result + complete() (fencing token)
  échec retryable (overload/timeout/vide) → fail(retryable) → requeue backoff
  heartbeat/lease : un thread prolonge les leases ; recover_on_boot au démarrage
```

**Backpressure `M`** : contrôleur AIMD double-signal (réutilise la classif `llm.py:144-168`) —
signal dur (LlmFailure overload/internal/vide → `M ×0.5`), signal doux (latence p50/p95 →
gel de l'increase), increase prudent (`M +1` jusqu'à plafond). Couplé au claim resource-aware.

## 4. Composants

**Nouveaux** (`trader/queue/` + `trader/application/`) :
- `decide_worker.py` (ou dans `queue/`) : le **pool de workers** decide + son cycle de vie
  (démarrage au boot, heartbeat/lease, arrêt propre). Réutilise `Worker`/`ResourcePools`.
- `decide_one.py` : le **handler** — construit le prompt 1-symbole (extrait de `decide_batch`),
  appelle acpx, retourne une `Decision`. Réutilise le contexte par-symbole existant (`per_symbol`).
- `decide_dispatch.py` (dans `run_cycle`) : **producteur + collecte** — enfile les tâches du
  cycle, attend (budget), collecte les `Decision`, applique la politique de skip.

**Réutilisés (inchangés)** : `task_ledger` / `pools` / `worker` (Phase 0) ; la classif
d'erreurs et le lifecycle acpx `llm.py` ; `order_admission` + `SimBroker` (exécution) ;
le gate, `armed_decisions`, recording, scheduler.

**Ledger** : réutilise `state/task_ledger.db` (Phase 0). `recover_on_boot()` repasse les
`running` orphelins (lease expirée) en `pending`.

## 5. Ce qui NE change PAS

Exécution des ordres, gate de pertinence, `armed_decisions` (court-circuitent le LLM),
recording (`decisions.jsonl`), scheduler, consolidation, backend d'état (reste `json` par
défaut). Le chemin `_batch_decide` reste présent comme fallback (flag off).

## 6. Invariants (tests TDD)

1. Flag off → comportement `_batch_decide` **strictement inchangé** (non-régression).
2. Flag on → chaque symbole décidable engendre **exactement une** tâche `decide`
   (`uniq_active_kind_partition` : pas de double du même symbole).
3. Une tâche `decide` qui échoue (retryable) est **requeue** (pending, backoff), pas HOLD.
4. Budget écoulé → symboles non finis **skippés** (pas de décision fabriquée) ; leurs tâches
   restent `pending`.
5. Décisions collectées via file == décisions du chemin batch pour le **même contexte**
   (parité fonctionnelle du `decide_one` vs `decide_batch` sur 1 symbole — modulo
   non-déterminisme LLM, testé sur un client mocké déterministe).
6. Crash pendant `decide` → au reboot, `recover_on_boot` remet les `running` en `pending`
   (pas de décision perdue ni doublée).
7. Backpressure : sous erreurs overload simulées, `M` décroît ; série saine, `M` remonte
   (bornes respectées).

## 7. Risques & points ouverts

- **Refactor `decide_batch` → `decide_one`** : le prompt 1-symbole doit être fidèle au batch
  (contexte par-symbole déjà disponible ; vérifier qu'aucune info « comparative » n'était
  réellement utilisée — le design §4.3bis argue que non). Point de vigilance en review.
- **Coût acpx** : plus d'appels (1/symbole) — atténué par réveils intelligents (peu de
  symboles dus/tick) + prompt caching (préfixe commun). À mesurer flag-on.
- **Attente du cycle** : le polling de collecte ne doit jamais bloquer > budget ; réveil sur
  complétion pour ne pas gaspiller le budget en sleep.
- **Cohabitation avec le shadow** (Phase 2) : le shadow tourne toujours en fin de cycle ;
  s'assurer que `decide`-via-file et le shadow n'écrivent pas la même DB (ledgers distincts :
  `task_ledger.db` vs `shadow_queue.db`).
