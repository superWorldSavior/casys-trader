# Référence — File de tâches durable (task-ledger)

> **Type** : Reference (Diátaxis).
> **Code** : primitives `infrastructure/queue/ledger`, `infrastructure/queue/pools`, `infrastructure/queue/worker` ; dispatchers `application/decide/queue_dispatch` + `application/execute/queue_dispatch`, payload plans `application/execute/queue_plan` ; bootstrap runtime `runtime/queue_runtime` + `runtime/data_source_runtime`, orchestration `runtime/daemon`, handlers `application/decide/handler` + `application/execute/order_handler`, backend `infrastructure/state_db/*` (outbox). Les anciens imports `trader.queue.*` et `trader.state_db.*` restent compatibles via alias virtuels.
> **Statut** : ✅ **File durable ACTIVÉE en paper** — SQLite est l'état paper canonique, sans flag de backend. `CASYS_QUEUE_DECIDE_ENABLED` et `CASYS_QUEUE_EXECUTE_ENABLED` restent les deux commutateurs opérationnels des chemins queue. Le comparateur JSON/SQLite a été retiré du cycle le 2026-07-10 ; les anciens JSON sont figés et ne constituent plus une vérité runtime.
> **Rôle** : file durable qui découple la production des tâches de leur traitement (durabilité, reprise, idempotence, backpressure).

## `TaskLedger` (`infrastructure/queue/ledger`)

Persistance SQLite (WAL, `busy_timeout=5000`). Une connexion partagée
(`check_same_thread=False`) protégée par un `threading.Lock`, sur le modèle de
`agent/learnings/store.py`. Temps injecté (`now_ms`) pour déterminisme. Tous les
timestamps = epoch ms (int).

### Table `tasks`

| Colonne | Type | Rôle |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | identifiant interne |
| `kind` | TEXT | type de tâche (`refresh_symbol`, `decide`, `execute_order`…) |
| `dedup_key` | TEXT UNIQUE | clé d'idempotence ; `None` = pas de dédup |
| `partition_key` | TEXT | clé de sérialisation (ex. symbole, `"portfolio"`) |
| `resource` | TEXT | ressource externe requise (`acpx` / `yahoo` / `portfolio` / NULL) |
| `priority` | INTEGER | 0 = EXIT (urgent) … 9 = MAINTENANCE (défaut : ASC) |
| `status` | TEXT | `pending` → `running` → `done` / `dead` |
| `attempts` / `max_attempts` | INTEGER | compteurs de tentative (défaut max = 3) |
| `scheduled_at` | INTEGER | epoch ms ; claim refusé avant cette heure |
| `lease_expires_at` | INTEGER | epoch ms ; expiration du bail (reprise au boot) |
| `claim_token` | TEXT | fencing token : garde `complete`/`fail`/`heartbeat` |
| `claimed_by` | TEXT | identifiant du worker |
| `result` / `error` | TEXT | sortie ou message d'erreur |

**Index** :

| Index | Définition | Rôle |
|---|---|---|
| `idx_claim` | `(status, priority, scheduled_at, id)` | tri du claim |
| `uniq_running_partition` | `(partition_key) WHERE status='running' AND partition_key IS NOT NULL` | ≤ 1 tâche running par clé |
| `uniq_active_kind_partition` | `(kind, partition_key) WHERE status IN ('pending','running') AND partition_key IS NOT NULL` | anti-ré-enfilage du même symbole |

### Méthodes

| Méthode | Comportement |
|---|---|
| `enqueue(*, kind, priority, scheduled_at_ms, now_ms, dedup_key, …)` | Insère en `pending`. Idempotent : `ON CONFLICT(dedup_key) DO NOTHING` → retourne `int(id)` ou `None` sur conflit. |
| `claim(*, worker_id, token, now_ms, lease_ms, free_resources)` | Transaction `BEGIN IMMEDIATE`. Sélectionne la tâche `pending` la plus prioritaire (`priority ASC, scheduled_at ASC, id ASC`) avec `scheduled_at ≤ now_ms`, `attempts < max_attempts`, ressource dans `free_resources` (ou NULL), et `partition_key` sans tâche `running` concurrente. Passe en `running`, pose `claim_token` et `lease_expires_at`. Retourne `dict` ou `None`. |
| `complete(*, task_id, token, now_ms, result)` | Passe en `done`, efface le claim. Gardé par fencing token (`claim_token` + `status='running'`). Retourne `True` si modifié. |
| `fail(*, task_id, token, now_ms, error, retryable, backoff_base_ms)` | Gardé par fencing token. Si `retryable=True` et `attempts < max_attempts` : repasse en `pending` avec `scheduled_at = now_ms + backoff_base_ms × 2^(attempts-1)`. Sinon : `dead`. Retourne `'pending'`, `'dead'` ou `'stale'` (token inconnu — sans effet). |
| `heartbeat(*, task_id, token, now_ms, lease_ms)` | Prolonge `lease_expires_at`. Gardé par fencing token. Retourne `True` si renouvelé. |
| `recover_on_boot(*, now_ms)` | Repasse en `pending` les tâches `running` dont `lease_expires_at < now_ms` (orphelines d'un crash). Retourne le nombre de tâches réactivées. |
| `release_claim(*, task_id, token, now_ms)` | Annule un claim **sans consommer de tentative** (`attempts = MAX(0, attempts-1)`) — pour les resource-miss. Remet en `pending`, ne touche pas `scheduled_at`. Retourne `True` si modifié. |

## `ResourcePools` (`infrastructure/queue/pools`)

Bornage de concurrence par ressource nommée (`acpx` / `yahoo` / `portfolio`).
Chaque ressource a un plafond initial (`_max`) et une limite effective courante
(`_eff`) modulée par AIMD.

### AIMD

| Signal | Effet |
|---|---|
| `on_overload(resource)` | `_eff = max(1, _eff // 2)` ; remet le streak à 0 |
| `on_success(resource)` | Incrémente le streak ; quand `streak ≥ COOLDOWN_SUCCESSES` (= 3), `_eff = min(_max, _eff + 1)` |

Le streak est initialisé à `COOLDOWN_SUCCESSES` (sans overload préalable,
chaque succès incrémente `_eff` directement).

### Interface

| Méthode | Rôle |
|---|---|
| `free_resources() -> list[str]` | Ressources avec au moins un slot libre (`_used < _eff`) |
| `try_acquire(resource) -> bool` | Non-bloquant : incrémente `_used` si slot disponible |
| `release(resource)` | Décrémente `_used`, notifie les waiters. Sur over-release : log warning, pas d'exception. |
| `wait_for_free(timeout)` | Bloque sur `Condition.wait_for` jusqu'à ce qu'un slot soit libre |
| `effective_limit(resource) -> int` | Lit `_eff` courant |

`Condition` (non-réentrant) : le prédicat de `wait_for_free` accède
directement à `_used`/`_eff`/`_max` (pas via `free_resources()`) pour éviter
un deadlock.

## `Worker` (`infrastructure/queue/worker`)

Boucle de traitement : claim → acquire ressource → handler → complete/fail → release.

```
run_once(*, now_ms, token) -> bool
```

Séquence dans `run_once` :

1. `pools.free_resources()` → liste des ressources disponibles.
2. `ledger.claim(free_resources=…)` → tâche ou `None` (→ `return False`).
3. `pools.try_acquire(resource)` : si resource miss (course), `ledger.release_claim(…)` sans brûler de tentative → `return True`.
4. `handlers[kind](task)` :
   - Succès → `ledger.complete(…)` + `pools.on_success(resource)`.
   - `RetryableError(is_overload=True)` → `pools.on_overload(resource)` + `ledger.fail(retryable=True, …)`.
   - `RetryableError(is_overload=False)` → `ledger.fail(retryable=True, …)` (pas de signal AIMD — erreur transitoire sans rapport avec la charge).
   - `Exception` (non retryable) → `ledger.fail(retryable=False, …)`.
5. `finally` : `pools.release(resource)` toujours exécuté.

Retourne `True` si une tâche a été tentée (y compris resource-miss), `False` si
rien à claimer.

### `RetryableError`

```python
RetryableError(*args, is_overload: bool = False)
```

Le handler lève `RetryableError` pour signaler un échec transitoire et déclencher
un requeue avec backoff. `is_overload=True` uniquement si la ressource externe
est saturée (rate-limit, queue pleine) — active la descente AIMD. Ne pas le
poser pour les timeouts réseau ponctuels.

## Invariants clés

- **Idempotence** : `enqueue` avec le même `dedup_key` est silencieux → `None`.
- **≤ 1 running par `partition_key`** : garanti à la fois par le claim SQL et par l'index `uniq_running_partition` (défense en profondeur).
- **Fencing token** : `complete`/`fail`/`heartbeat` échouent en silence sur token périmé.
- **Resource-aware claim** : un claim ne passe jamais sur une ressource sans slot libre.
- **Retry + backoff exponentiel** : `scheduled_at = now + base × 2^(attempts-1)` jusqu'à `max_attempts` → `dead`.
- **Reprise au boot** : `recover_on_boot` remet en `pending` les `running` à bail expiré (orphelins d'un crash précédent).
- **Unclaim neutre** : `release_claim` sur resource-miss ne consomme pas de tentative ni n'applique de backoff.

## Pipeline `decide` / `execute` via la file (Phase 3)

Le daemon route deux étages de `run_cycle` par la file durable, chacun derrière un
**flag orthogonal** (off = comportement historique strictement inchangé). Les 4
combinaisons sont valides et testées.

Répartition runtime :

- `runtime/daemon.py::main()` reste propriétaire de la lecture env/CLI, du
  shutdown des pools et du handoff vers `run_cycle` (`queue_decide_enabled`,
  `task_ledger`, `queue_execute_enabled`, `execute_ledger`) ;
- `runtime/queue_runtime.py` construit les objets concrets de boot : ledgers,
  resource pools, worker pools, handlers et stack SQLite partagée de
  `execute_order`.

### Backend d'état — SQLite canonique

Le daemon utilise toujours `state/casys.db` (WAL) pour le broker, les plans, le
scheduler et l'outbox. Il ne lit plus `CASYS_STATE_BACKEND` : un retour runtime à
`json` serait incohérent puisque le store de plans JSON a été supprimé.

`bootstrap_state_backend` conserve l'import one-shot **idempotent** des anciens
JSON, protégé par les sentinels `state_imports`, afin d'amorcer une base neuve ou
un environnement de test. Les fichiers `broker.json`, `trade_plans.json` et
`scheduler.json` ne sont plus régénérés ni double-écrits après la bascule. Les
adaptateurs broker/scheduler JSON restent appelables explicitement pour les tests
et la récupération historique, mais ne sont plus un mode du daemon.

### Étage `decide` — `CASYS_QUEUE_DECIDE_ENABLED`

- off : `_batch_decide` synchrone (ThreadPoolExecutor), inchangé.
- on : `dispatch_decide_via_queue` enfile **1 tâche `decide` par symbole**
  (`partition_key=symbole`, `resource=acpx`, `dedup_key=cycle:sym`) ; le `DecidePool`
  (`task_ledger.db`) les draine, `run_cycle` attend les états terminaux puis
  collecte. Symboles `dead` ou `running` au lease expiré =
  **skippés** (pas de HOLD synthétique) → fin du HOLD-par-saturation. Le nb de
  workers = `CASYS_DECISION_BATCH_PARALLELISM` (même flag, sens différent du mode
  batch). `CASYS_DECISION_BATCH_SIZE` est **sans objet** en queue (grain-symbole) —
  warning au boot. **Tour d'outils / session free-iteration** : avec
  `CASYS_AGENT_TOOLS` actif, le handler ouvre une session acpx persistante et
  l'agent demande autant de tournées d'outils que nécessaire, puis décide dès qu'il
  a assez de contexte (`get_indicator_context`, `get_active_plans`,
  `recall_learnings`, `get_freshness` — 8 calls/symbole/round). Le code garde
  seulement un backstop anti-runaway `SESSION_ROUND_BACKSTOP=20`, non exposé comme
  budget fonctionnel. **No call cap** : pas de limite d'admission par appels en
  queue ; `model_calls_used` est une métrique d'observabilité, pas une limite. Le
  coût est borné par le timeout par appel, `ResourcePools` AIMD, le traitement async
  qui reporte les non-finies, et l'univers borné. Le timeout est piloté par
  `CASYS_DECISION_TIMEOUT_S` (`.env` paper : 240 s au 2026-07-06 ; défaut code :
  900 s). Le lease decide session est court
  (intervalle inter-heartbeat, typiquement `(decision_timeout_s + 30) * 2`) et le
  worker renouvelle via heartbeat après l'ouverture de session puis après chaque
  appel modèle. REQUEST_CONTEXT legacy reste désactivé (le tool round moderne est
  la voie de recherche de contexte). Fin du mode dégradé Lot A.
- Avant enqueue, `application/decide/context_projection.py` transforme le
  snapshot global en `decision_focus_v1` : risque exact de la cible, briefing
  local Univers/micro/news, radar borné, résumés globaux et détail via outils.
  Le snapshot source n'est jamais muté et chaque tâche reçoit sa propre vue.
- Le premier appel de chaque session ACP transporte cette vue complète. Les tours
  suivants de la même session transportent uniquement les nouveaux
  `tool_results`. Si le runner change de backend, sa nouvelle session repart du
  prompt complet avant toute continuation delta.

### Services tool-round grain-1

`runtime/queue_runtime.build_decide_tool_services()` construit un
`ToolRoundServices` injecté au handler `decide` :

| Service | Rôle |
|---|---|
| `get_bars` | wrapper indirect vers la data source courante, sans capture d'objet runtime |
| `worker_cycle_context` | handle unique des plans, inputs de validation et attribution complète du cycle |

Le daemon possède le contexte partagé via `WorkerCycleContextHandle`. À chaque
cycle, `run_cycle` définit `cycle_id = now.isoformat()`, construit
`WorkerCycleContext(cycle_id, as_of, open_plans, exit_validation)` puis le publie.
Après les sorties mécaniques de début de cycle, l'attribution est calculée et
ajoutée atomiquement au même snapshot. Le payload `decide` transporte ce
`cycle_id`; `decide_one` fabrique les closures `get_active_plans`,
`get_attribution` et `strategy_exit` bornées à ce cycle.

Le handler `get_active_plans` lit `OpenPlansSnapshot.rows` : sa portée est
globale, puis `symbol` filtre si demandé. Si un worker lent demande un cycle qui
n'est plus courant, le handle lève `CycleContextUnavailable`; le runner d'outils
le transforme en tool error au lieu de lire le cycle N+1. Le validateur
`strategy_exit` échoue fermé avec `cycle_context_unavailable`.

Frontière de validation :

- `application.exit_update.validate_exit_update()` est un dry-run pur : il lit un
  store snapshot, résout l'update, mais n'appelle jamais `upsert`.
- `application.exit_update.apply_exit_update_to_open_plan()` reste le chemin
  persistant côté daemon après décision finale.
- `action_validator` ne couvre que `strategy_exit`. Les entrées et sorties marché
  (`strategy_entry`, `strategy_close`) restent sous les gates du daemon
  (`cycle_decision`/risk/broker).
- Si le worker n'a pas les barres daily nécessaires et que le seul rejet serait
  `hard_stop_bars_unavailable`, il s'abstient pour éviter un faux rejet ; le
  daemon tranchera avec les barres du cycle.
- Si le dry-run rejetterait réellement le `strategy_exit`, `resolve_symbol_decision`
  réinjecte un `tool_results` `{tool:"strategy_exit", ok:false, error:<reason>}`
  dans la même session, jusqu'à 2 corrections avant fall-through.

### Étage `execute` — `CASYS_QUEUE_EXECUTE_ENABLED` (exige `sqlite`)

- off : `SimBroker.submit` synchrone dans `run_cycle`, inchangé.
- on : `run_cycle` assemble le contexte runtime, délègue la préparation du payload
  atomique `plan_to_upsert` / `symbol_to_close` à `application.execute_queue_plan`,
  puis délègue à `application.execute_queue_dispatch`, qui enfile **1 tâche `execute_order`** par
  ordre retenu (`resource=portfolio`, sérialisé). Le worker exécute **submit +
  plan + `done` dans UNE transaction SQLite** (outbox, `execute_order_unit`). Le Fill est écrit **dans
  la même tx** (`complete_in_tx`) → aucune perte de fill au crash. Fencing (`SELECT
  status/token` avant submit) → aucun double-fill au rejeu. `dead`/timeout →
  **fail-closed** (`executed=False`, jamais loggé « ok »). `dedup_key`
  `exec:{cycle}:{sym}:{intent}`. Broker, plan et ledger partagent toujours la même
  `casys.db` canonique.

### Orchestration decide → execute (streaming, sous itération libre)

Sous l'itération libre (décisions de durée variable), `run_cycle` ne collecte plus
tout le lot avant d'exécuter — sinon l'exécution serait otage du symbole le plus
lent. À la place :

- **Collecte événementielle** : `dispatch_decide` expose `iter_decide_results_via_queue`
  qui **yield chaque décision dès son état terminal** (done → décision, dead/lease-expiré
  → `undecided`), sans attendre le reste. `budget_s` a été supprimé (doublon du timeout
  par appel) : on **attend jusqu'à terminal**, un worker mort expire vite (lease court +
  heartbeat par round), un résultat tardif est collecté, plus jeté.
- **Sorties streamées** : `CLOSE`/`REDUCE` (intent **résolu** position-aware) → exécutées
  **immédiatement**, refresh marge — libèrent de la marge sans attendre le lot.
- **Ouvertures bufferisées** : `OPEN_*`/`SCALE_IN`/`FLIP` (FLIP = ouverture, jamais
  streamé) → exécutées ensuite en `gross_execution_order` (arbitrage au mérite préservé,
  voit la marge refreshée par les sorties). Budget des ouvertures = le RiskGate/marge (le
  cap de count `max_orders_per_cycle` a été retiré : les bornes $ sont la safety).
- **Garde timeout** : un `execute_order` timeouté est **abandonné** (`ledger.abandon` →
  `dead`) ; le **fence early** de l'UoW empêche toute soumission tardive au broker. Course
  done/abandon fermée (relecture + crédit du fill). Une **ouverture** timeoutée stoppe le
  reste du batch d'ouvertures (`undecided`).

### Observabilité

- Logs `[queue_dispatch]` (producteur : `symbols/decided/undecided/skipped`),
  `[queue_decide]` / `[queue_execute]` (pools), `[queue.ledger]` / `[queue.worker]`.
- Le comparateur historique reste disponible **uniquement à la demande** avec
  `python -m trader.infrastructure.state_db.compare state`. Comme les JSON sont
  figés, une divergence après la bascule est attendue et ne doit pas être utilisée
  comme alarme de santé. Le daemon ne lance plus cette lecture à chaque cycle.
- La sonde no-op `shadow_queue.db` et son flag `CASYS_SHADOW_QUEUE_ENABLED` ont été
  retirés du runtime et de la configuration paper. Le module historique n'est pas
  invoqué par le daemon.

### Activation / rollback opérationnel

Les flags `CASYS_QUEUE_DECIDE_ENABLED` et `CASYS_QUEUE_EXECUTE_ENABLED` peuvent
encore sélectionner leurs chemins synchrones de secours après redémarrage. Il
n'existe plus de rollback de l'état vers JSON : sauvegarde, restauration et
diagnostic doivent porter sur `state/casys.db`.

### État (2026-07-10) et gommage restant

- **Bascule état terminée** : SQLite est le défaut des factories et l'unique backend
  du daemon ; double-write, comparaison automatique et flags backend/shadow retirés.
- **Gommage restant** : observer puis retirer séparément les fallbacks synchrones
  `decide` et `execute`. Leurs deux flags restent donc intentionnellement présents
  jusqu'à validation de ces suppressions.

## Voir aussi

- Conception générale + phases : [`../superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md`](../superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md)
- Conception Phase 3 (decide + execute via file, flags) : [`../superpowers/specs/2026-07-04-phase3-decide-execute-via-file-design.md`](../superpowers/specs/2026-07-04-phase3-decide-execute-via-file-design.md)
- Contexte agent (`now_human`, `market_clocks`) : [`agent-context.md`](agent-context.md)
- Registre de décisions : [`../decisions/registre-decisions-metier.md`](../decisions/registre-decisions-metier.md)
