# Référence — File de tâches durable (task-ledger)

> **Type** : Reference (Diátaxis).
> **Code** : `queue/ledger`, `queue/pools`, `queue/worker`
> **Statut** : ⚠️ **Phase 0 — cœur en place, NON branché au daemon** (`CASYS_QUEUE_ENABLED` n'existe pas encore). Décrit le comportement du module ; l'intégration (producteur, handlers `decide`/`execute`) arrive en Phase 1-3.
> **Rôle** : file durable qui découple la production des tâches de leur traitement.

## `TaskLedger` (`queue/ledger`)

Persistance SQLite (WAL, `busy_timeout=5000`). Une connexion partagée
(`check_same_thread=False`) protégée par un `threading.Lock`, sur le modèle de
`learnings/store.py`. Temps injecté (`now_ms`) pour déterminisme. Tous les
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

## `ResourcePools` (`queue/pools`)

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

## `Worker` (`queue/worker`)

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

## Voir aussi

- Spec de conception et contexte des phases : [`../superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md`](../superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md)
- Plan d'implémentation Phase 0 : [`../superpowers/plans/2026-07-03-task-ledger-lot-a-phase0.md`](../superpowers/plans/2026-07-03-task-ledger-lot-a-phase0.md)
- Registre de décisions : [`../decisions/registre-decisions-metier.md`](../decisions/registre-decisions-metier.md)
