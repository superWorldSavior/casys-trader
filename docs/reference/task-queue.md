# Référence — File de tâches durable (task-ledger)

> **Type** : Reference (Diátaxis).
> **Code** : primitives `infrastructure/queue/ledger`, `infrastructure/queue/pools`, `infrastructure/queue/worker` ; dispatchers `application/queue_dispatch` + `application/execute_queue_dispatch`, payload plans `application/execute_queue_plan` ; bootstrap runtime `runtime/queue_runtime` + `runtime/data_source_runtime`, orchestration `runtime/daemon`, handlers `application/decide_handler` + `application/execute_order_handler`, backend `infrastructure/state_db/*` (outbox). Les anciens imports `trader.queue.*` et `trader.state_db.*` restent compatibles via alias virtuels.
> **Statut** : ✅ **Phase 3 ACTIVÉE en paper (2026-07-04)** — les 3 flags on (`CASYS_STATE_BACKEND=sqlite`, `CASYS_QUEUE_DECIDE_ENABLED`, `CASYS_QUEUE_EXECUTE_ENABLED`), migration d'état validée, `[state-compare] identical=True`. Les chemins synchrones historiques restent présents comme fallback (flags off) jusqu'au gommage strangler. Voir la section « Pipeline » ci-dessous.
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

### Backend d'état — `CASYS_STATE_BACKEND` (`json` | `sqlite`)

- `json` (défaut historique) : `SimBroker` / `TradePlanStore` / `Scheduler` sur fichiers.
- `sqlite` : mêmes API, état unifié dans `state/casys.db` (WAL). Migration one-shot
  **idempotente** au boot (`bootstrap_state_backend`) : import des JSON existants +
  sentinels `state_imports` (anti-résurrection d'un état déjà migré) + régénération
  des **shadows JSON**. Le shadow (double-write JSON après chaque mutation) est un
  filet transitoire pour les lecteurs hors-store encore branchés sur les fichiers
  (cockpit/TUI/CLI/stats/rotation). Prérequis de l'outbox execute.

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

### Services tool-round grain-1

`runtime/queue_runtime.build_decide_tool_services()` construit un
`ToolRoundServices` injecté au handler `decide` :

| Service | Rôle |
|---|---|
| `get_bars` | wrapper indirect vers la data source courante, sans capture d'objet runtime |
| `open_plans_provider` | provider global des `TradePlan` ouverts sérialisés pour `get_active_plans` |
| `open_plans_as_of_provider` | horodatage du snapshot renvoyé dans `{rows, as_of}` |
| `action_validator` | dry-run pré-exécution des `strategy_exit` proposés par l'agent |

Le daemon possède le snapshot partagé via `PlanSnapshotHandle`. À chaque cycle,
il charge `plan_store.open_plans()`, stocke les vrais plans dans
`PlanSnapshotHandle.get_raw()` et la version contexte dans `get()`, avec `as_of`.
Le handler `get_active_plans` lit `get()` : sa portée est globale, puis `symbol`
filtre si demandé.

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
  `exec:{cycle}:{sym}:{intent}`. Si `CASYS_STATE_BACKEND != sqlite`, le flag est
  ignoré avec warning (broker/plan/ledger doivent partager la même `casys.db`).

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

- **`[state-compare]`** (fin de cycle, sous sqlite) : `compare_backends` compare
  `casys.db` ↔ shadows JSON et logge `identical=… cash=… positions=N plans=N wakes=N
  watches=N stale=N`. `identical=True` = zéro dérive ; toute divergence part en
  warning avec le détail. **C'est le filet de la bascule.**
- Logs `[queue_dispatch]` (producteur : `symbols/decided/undecided/skipped`),
  `[queue_decide]` / `[queue_execute]` (pools), `[queue.ledger]` / `[queue.worker]`.
- **Sonde `[shadow-queue]`** (`CASYS_SHADOW_QUEUE_ENABLED`) : **OBSOLÈTE** — elle
  drainait un handler no-op dans `shadow_queue.db` pour valider la mécanique *avant*
  bascule. Désactivée (=0) depuis l'activation de la vraie file ; à retirer au gommage.

### Activation / rollback

Chaque flag est un strangler **réversible**. Rollback = repasser le(s) flag(s) à
`0`/`json` dans `.env` puis **redémarrer** le daemon (`load_dotenv` est lu au boot,
`daemon.py` `main()`). Le double-write shadow JSON garde l'état JSON à jour en
parallèle du SQLite → retour arrière sans perte d'état.

### État (2026-07-04) et gommage strangler à venir

- **Activé en paper**, migration validée, `[state-compare] identical=True`. Le vrai
  trafic `decide`/`execute` via file s'observe à la réouverture des marchés (un boot
  frais week-end donne `due=0`).
- **Gommage** (dette, **gated par validation**, ordre contraint) : (1) migrer les
  ~10 lecteurs JSON hors-store → SQLite ; (2) retirer le double-write + basculer le
  défaut `sqlite` ; (3) retirer le fallback `decide` synchrone ; (4) retirer le
  fallback `execute` synchrone ; (5) retirer la sonde shadow. Les lecteurs d'abord :
  ils bloquent le retrait du double-write.

## Voir aussi

- Conception générale + phases : [`../superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md`](../superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md)
- Conception Phase 3 (decide + execute via file, flags) : [`../superpowers/specs/2026-07-04-phase3-decide-execute-via-file-design.md`](../superpowers/specs/2026-07-04-phase3-decide-execute-via-file-design.md)
- Contexte agent (`now_human`, `market_clocks`) : [`agent-context.md`](agent-context.md)
- Registre de décisions : [`../decisions/registre-decisions-metier.md`](../decisions/registre-decisions-metier.md)
