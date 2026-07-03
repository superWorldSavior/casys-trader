# File de tâches durable in-process (`task_ledger.db`)

**Date** : 2026-07-03
**Statut** : 📐 **DESIGN VALIDÉ + RÉVISÉ POST-CODEX** (brainstorming) —
implémentation non commencée.
**Approche retenue** : ① Task-ledger SQLite in-process, threads, migration
*strangler*. Fait-main (zéro dépendance), patterns empruntés au SOTA durable
execution.
**Revue** : 2 sessions Codex (axes *architecture/concurrence SQLite* et *safety
trading/migration*), 2026-07-03. Verdicts : arch = GO-AVEC-CORRECTIFS ; safety =
STOP sur la partie ordre/live tant que la machine d'état externe, la corrélation
IB, la migration idempotente et l'ownership unique ne sont pas spécifiés. Tous les
correctifs sont intégrés ci-dessous ; la scission **Lot A / Lot B** (§0) répond au
STOP.

> **Portée** : remplace le pilotage du daemon (boucle `while-True` synchrone +
> `scheduler.json`) par une **file de tâches durable** adossée à SQLite, et migre
> l'état muté (broker, trade_plans, scheduler) dans le **même substrat** pour
> obtenir des transactions ACID réelles. Objectifs : robustesse au crash,
> reprise/idempotence, voie rapide pour les sorties, isolation par symbole, et
> récupération du débit LLM aujourd'hui bridé.

---

## 0. Découpage en deux lots (réponse au STOP safety)

Le verdict STOP ne porte QUE sur l'exécution d'ordres. On livre en deux lots
indépendants, alignés sur le strangler :

- **Lot A — Socle file + durabilité de l'état (GO).** File SQLite durcie ;
  migration `broker`/`trade_plans`/`scheduler` en SQLite (tue RC-1/2/4) ; `decide`
  via file produisant des **intentions** ; resource-aware claim ; fencing/lease ;
  backpressure adaptative. **La file ne passe AUCUN ordre** — l'exécution reste sur
  le chemin synchrone actuel. Le gros des races critiques meurt ici.
- **Lot B — Exécution d'ordres via file (nécessite que §7 soit spécifié et validé
  avant impl).** Machine d'état `external_orders`, corrélation + réconciliation IB,
  `order_intents` (ownership exclusif), `trade_plan` activé **après** fill.

**Aucune ligne de Lot B ne s'implémente tant que §7 n'est pas revalidé.**

## 1. Contexte & problème

Le daemon est une boucle `while True:` **monothread, synchrone** (`daemon.py:2829`),
qui poll toutes les ~30 s, lit `scheduler.json` pour les symboles « dus », et
exécute un cycle complet d'un bloc via `run_cycle()` (`daemon.py:1455`).

Le batch de décision (`planner_batch.py:152`) découpe l'univers en chunks (1 chunk
= 1 appel LLM) via un `ThreadPoolExecutor` (`planner_batch.py:372`) dimensionné par
`CASYS_DECISION_BATCH_PARALLELISM` (défaut 3, `planner_batch.py:20`), **forcé à 1**
(`.env.example:25`) depuis l'overload app-server du 29-30/06. Les threads du batch
**n'écrivent aucun fichier d'état** ; l'écriture est séquentielle dans le thread
principal.

Le diagnostic (3 audits, 2026-07-03) n'est **pas** un nid de races entre threads —
le monothread sérialise déjà les mutations. Ce sont **durabilité, reprise et
idempotence** qui manquent :

| # | Défaut | Nature | Gravité | Localisation |
|---|--------|--------|---------|--------------|
| RC-1 | `broker.json` en `write_text()` direct | non-atomique → JSON corrompu au crash | **Critique** | `execution.py:223-225` |
| RC-2 | `trade_plans.json` en `write_text()` direct | idem → plans stop/TP perdus | **Critique** | `trade_plan.py:1019-1021` |
| RC-4 | `scheduler.json` en `write_text()`, N sauvegardes/cycle | réveils/veilles perdus | Haute | `scheduler.py:42-44` |
| RC-5 | crash entre `remove_watch()` et exécution d'un ordre armé | ordre perdu **ou** double | Haute | `daemon.py:1819` |
| RC-3 | TOCTOU `daemon.pid` : 2 daemons possibles | vraie race | Haute | `pid_file.py:34-45` |
| A | reap croisé des bridges `codex-acp` sous `parallelism>1` | vraie race | Haute (mitigée `=1`) | `llm.py:268-313` |
| E | pas de checkpoint intra-cycle | décisions rejouées, sans continuité | Moyenne | `run_cycle` |

Débit : à saturation acpx, `decide` tombe en `LlmFailure` **non-retryable** →
`HOLD` synthétique **définitif** (`llm.py:358`). Le `timeout` (900 s) est aussi
non-retryable.

Sain déjà en place (à préserver) : dédup `decision_id` + `os.replace` atomique sur
`decisions.jsonl` (`decision_ledger.py:169`, `:267-269`) ; `learnings.db` WAL +
`threading.Lock` + `busy_timeout` + `check_same_thread=False`
(`learnings/store.py:26,31,107`) ; backoff LLM (`LlmRouter`) et stale-data.

## 2. Objectif

File de tâches durable in-process, sans Docker ni service externe :
1. **Robustesse au crash** — état en SQLite (ACID), reprise propre, idempotence.
2. **Voie rapide sorties** — jamais bloquées par un batch LLM.
3. **Isolation par symbole** — un symbole qui plante ne retarde pas les autres.
4. **Débit récupéré** — backpressure adaptative + requeue au lieu de HOLD.

Non-objectifs (YAGNI) : distribution multi-process, broker de messages, workers
externes (daemon stateful côté IB — cf. §3).

## 3. Décisions (validées)

- **Fait-main SQLite, pas de lib/moteur.** SOTA 2026 : Celery/RQ/Dramatiq/arq/
  taskiq/procrastinate/pgqueuer exigent Redis ou Postgres → écartés (no-Docker).
  Temporal/Restate/Hatchet/Inngest/Windmill → serveur séparé, écartés. **DBOS
  Transact** est le seul moteur embarquable (SQLite dev) et reste l'upgrade
  transparent si Postgres entre en stack ; on emprunte ses patterns sans sa
  dépendance. `task_ledger.db` maison, même pattern que `learnings.db`.
- **Threads in-process, pas d'asyncio.** `ib-async` via API **synchrone**
  (`ib.connect`, `ib_source.py:405`) — gère sa loop en interne. SQLite multi-thread
  déjà maîtrisé (`learnings/store.py:107`). Travail **I/O-bound** → threads OK.
- **Migrer l'état dans le même `.db`.** `broker`/`trade_plans`/`scheduler` en tables
  → transactions ACID réelles. Fix de fond RC-1/RC-2/RC-4.
- **`decisions.jsonl` reste** (append-only + `os.replace`, journal d'audit).
- **Multi-process = non** (stateful côté IB) ; `task_ledger` reste le point de
  découplage si un broker REST stateless ouvre la porte plus tard.

## 4. Architecture — Lot A (socle)

### 4.1 Modèle d'exécution & concurrence

Un process. Le `while-True` devient un **producteur** : lit le scheduler (table),
enfile des tâches, dort. Un **pool de N worker-threads** consomme le ledger.

Trois mécanismes superposés :
1. **Pools de ressources** (sémaphores nommés) : `acpx` (limite **adaptative**
   AIMD, §4.5), `yahoo` (basse, anti-429), `ib` (**1** = verrou portefeuille).
   **Une seule ressource par tâche** — pas d'acquisition imbriquée (évite tout
   deadlock ; correctif arch MAJEUR 4).
2. **Sérialisation par clé (`partition_key`)** : au plus une tâche `running` par
   clé (symbole, ou `"portfolio"`). Imposée au claim ET par un invariant DB.
3. **GIL non bloquant** (I/O-bound).

### 4.2 Schéma `task_ledger.db`

WAL + `busy_timeout=5000` + `check_same_thread=False` + `threading.Lock`.
**Timestamps en epoch integer (ms UTC)** — jamais de TEXT comparé
lexicographiquement (correctif arch, trous).

```sql
CREATE TABLE tasks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT NOT NULL,
  dedup_key      TEXT UNIQUE,          -- idempotency key
  partition_key  TEXT,                 -- "AAPL" | "portfolio" | NULL
  resource       TEXT,                 -- acpx | yahoo | ib | NULL (UNE seule)
  priority       INTEGER NOT NULL,     -- 0=EXIT .. 9=MAINTENANCE
  payload        TEXT,
  status         TEXT NOT NULL,        -- pending | running | done | failed | dead
  attempts       INTEGER DEFAULT 0,
  max_attempts   INTEGER DEFAULT 3,
  scheduled_at   INTEGER NOT NULL,     -- epoch ms ; pas avant cette heure
  lease_expires_at INTEGER,            -- epoch ms
  claim_token    TEXT,                 -- fencing token (correctif arch MAJEUR 7)
  claimed_by     TEXT,
  enqueued_seq   INTEGER,              -- pour aging anti-famine
  parent_id      INTEGER,
  result         TEXT, error TEXT,
  created_at     INTEGER, updated_at INTEGER
);
CREATE INDEX idx_claim ON tasks(status, priority, scheduled_at, id);
-- Invariant DB en dur (défense en profondeur, correctif arch MAJEUR 1) :
CREATE UNIQUE INDEX uniq_running_partition
  ON tasks(partition_key)
  WHERE status='running' AND partition_key IS NOT NULL;
-- Anti ré-enfilage du même symbole à chaque poll (correctif arch, trous) :
CREATE UNIQUE INDEX uniq_active_kind_partition
  ON tasks(kind, partition_key)
  WHERE status IN ('pending','running') AND partition_key IS NOT NULL;
```

**Claim atomique** — en transaction `BEGIN IMMEDIATE` (correctif arch MAJEUR 1),
**resource-aware** (correctif arch MAJEUR 6 : ne claim que si un permit de la
ressource est libre, sinon les workers se piègent tous sur `acpx=1` et la voie
rapide meurt) :

```sql
BEGIN IMMEDIATE;
UPDATE tasks SET status='running', claimed_by=:w, claim_token=:token,
                 lease_expires_at=:lease, attempts=attempts+1, updated_at=:now
WHERE id = (
  SELECT id FROM tasks
  WHERE status='pending' AND scheduled_at <= :now
    AND attempts < max_attempts                    -- filtre les morts (arch, trous)
    AND (resource IS NULL OR resource IN (:free_resources))  -- resource-aware
    AND (partition_key IS NULL
         OR partition_key NOT IN (SELECT partition_key FROM tasks
                                   WHERE status='running' AND partition_key IS NOT NULL))
  ORDER BY priority ASC, scheduled_at ASC, id ASC   -- tie-break déterministe
  LIMIT 1)
RETURNING *;
COMMIT;
```

Le worker consomme/ferme le curseur avant `COMMIT`, gère `SQLITE_BUSY`, n'utilise
pas `rowcount`. `complete()/fail()` sont gardés par le fencing token :
`UPDATE ... WHERE id=:id AND claim_token=:token`.

**Réveil sur release** : `Condition.wait()`/notify sur libération de permit, pas un
simple polling 200 ms (correctif arch MAJEUR 6). Le polling reste le filet.

**Jamais de transaction SQLite ouverte pendant un appel acpx/yahoo/IB** (correctif
arch MINEUR 3 / MAJEUR 4) : on claim (tx courte), on relâche, on fait l'I/O, on
réécrit (tx courte).

### 4.3 Taxonomie des tâches, priorités

| `kind` | priorité | resource | partition | engendre |
|--------|----------|----------|-----------|----------|
| `apply_exits` | **0 (EXIT)** | ib¹ | portfolio | — |
| `scan_watches` | 0 | — | — | intention (Lot B) |
| `refresh_symbol` | 5 | yahoo **ou** ib² | symbol | `decide` |
| `decide` | 5 (DECISION) | acpx | symbol | **intention** (Lot B) |
| `arm_watch` | 5 | — | symbol | — |
| `consolidate_learnings` | **9 (MAINT)** | acpx | — | — |

¹ Lot B. ² `resource` = la source active du symbole (une seule), pas « yahoo/ib »
(correctif arch MAJEUR 4).

**Voie rapide** : `ORDER BY priority ASC` sert EXIT/EXEC avant DECISION.
**Anti-famine** (correctif arch 6 + safety 7) : *aging* — au-delà d'un seuil
d'attente (`enqueued_seq`), une tâche `decide` gagne en priorité effective ; et on
**ne lance pas** de nouvelle entrée tant qu'une sortie du même portefeuille est
`pending`.

### 4.4 `decide` produit une INTENTION, pas un ordre ni un plan

**Correctif majeur convergent (arch BLOQUANT + safety)** : le `trade_plan`
représente une **position ouverte** (créé *après* fill aujourd'hui,
`daemon.py:2628/2706`). Donc :

- `decide` (Lot A) → jeton `acpx` → LLM → écrit une **décision d'audit** et, si
  trade, une **intention** (`order_intents`, §7) `pending`. **Aucun plan, aucun
  ordre.**
- L'exécution de l'intention est du **Lot B** (§7). En Lot A, c'est le **chemin
  synchrone existant** qui exécute, en consommant `order_intents` (ownership
  exclusif, évite la double soumission — safety BLOQUANT 5).

### 4.5 Erreurs, retry, backpressure, reprise

**Deux gardes de temps DISTINCTES — ne pas les confondre** (décision 2026-07-03) :

- **Heartbeat + lease → panne « le daemon entier meurt ».** Un thread dédié
  prolonge `lease_expires_at` des tâches `running` tant que le daemon vit. S'il
  meurt (kill/OOM), les leases cessent d'être prolongées ; au boot,
  `recover_on_boot()` repasse en `pending` les `running` à lease expirée. **Un agent
  qui réfléchit longtemps n'est JAMAIS considéré orphelin tant que le daemon vit** —
  le heartbeat court dans un thread séparé du worker bloqué sur l'I/O. C'est ce qui
  permet de « laisser le temps qu'il faut » côté file.
- **Timeout du subprocess acpx → panne « un appel individuel se fige »** (bridge
  `codex-acp` zombie) sans tuer le daemon. Le heartbeat ne peut PAS le casser (le
  worker est vivant, bloqué dans `communicate()`, et tiendrait le jeton `acpx`
  limité à vie → la voie `decide` se gèle). On garde donc un timeout subprocess,
  mais **desserré et configurable par `kind`** (ex. `decide` 30-60 min) : l'agent a
  « le temps qu'il faut ». Son **dépassement devient un `requeue` (retryable), plus
  jamais un `HOLD` définitif** — la décision est reportée, pas perdue.
  *(Amélioration future : idle-watchdog — couper sur inactivité, pas sur durée —
  quand acpx exposera un flux de tokens ; aujourd'hui `--format quiet` +
  `communicate()` bloquant, `llm.py:106,299`, ne le permet pas.)*

- **Retry + backoff** : échec *retryable* (rate limit, overload, timeout desserré
  dépassé) → `pending`, `scheduled_at = now + base·2^attempts`, jusqu'à
  `max_attempts` → `dead`. `decide` idempotent (dedup) → rejeu sûr → **fin du
  HOLD-par-saturation**.
- **Backpressure adaptative `acpx`** (AIMD) : overload → concurrence ×0.5 ; succès
  → +1, plafonnée. Couplée au **claim resource-aware** (§4.2) pour ne pas piéger les
  workers.
- **Fencing token** : `complete()/fail()` gardés par `claim_token` → neutralise une
  double exécution résiduelle après reprise.
- **pid-lock atomique** (`O_EXCL` ou `fcntl.flock`) au boot → résout RC-3, condition
  de la reprise sûre.

## 5. Migration des stores d'état (Lot A) — idempotente

Backends SQLite pour `SimBroker` / `TradePlanStore` / `Scheduler`, **API publique
identique** (consommateurs inchangés). Migration one-shot au boot, **idempotente**
(correctif safety MAJEUR 4) :

- Table `schema_migrations` (version appliquée).
- Import JSON → tables **seulement si les tables sont vides** ; sinon skip.
- Contraintes `UNIQUE` (ex. un fill par `dedup_key`, un plan par `id`) pour bloquer
  tout doublon même si l'import est relancé.
- Backup horodaté des `.json` avant bascule.
- **SimBroker** : la mutation d'état **et** le `mark done` de la tâche/fill dans
  **une seule transaction** (correctif safety MAJEUR 3) → pas de double mutation au
  rejeu, même en paper.

## 6. Migration *strangler* (réversible par flag)

- **Phase 0 — Substrat débranché.** Module `queue/`, `task_ledger.db`, tests.
  Flag maître `CASYS_QUEUE_ENABLED=0`.
- **Phase 1 — État en SQLite, comportement inchangé** (§5). Mono-thread. →
  **RC-1/2/4 morts ici.**
- **Phase 2 — File en shadow.** Producteur enfile `refresh_symbol`+`decide`, on
  *compare* décisions/intentions file vs chemin synchrone (équivalence, risque nul,
  aucun ordre par la file).
- **Phase 3 (Lot A complet) — `decide`/`refresh` via file** ; l'exécution reste
  synchrone, consommant `order_intents`.
- **Phase 4 (Lot B) — exécution via file** : après §7 validé. `execute_order`,
  `external_orders`, réconciliation IB. Un flag, ancien chemin en fallback.
- **Phase 5 — Nettoyage** après observation (`measure_d7`).

**Réversibilité** (nuance safety MAJEUR 6) : réversible par flag **pour les classes
de tâches** ; pour le **substrat état**, « rollback » = *SQLite backend + queue
off*, PAS un retour au JSON (le backup JSON serait stale). Un export JSON
atomique/versionné est fourni comme filet, sans être le mode nominal.

**Observabilité** : vue cockpit `pending/running/dead/retry` par `kind`.

## 7. Lot B — Exécution d'ordres via file (À SPÉCIFIER/VALIDER AVANT IMPL)

> Cette section répond au STOP safety. Elle est **normative pour l'impl du Lot B**
> et doit être revue (Codex) avant tout code.

### 7.1 `order_intents` — ownership exclusif (safety BLOQUANT 5)

Table unique consommée par **exactement un** exécutant (ancien chemin OU file,
jamais les deux). `UNIQUE(intent_key)`, statut `open/claimed/executed/cancelled`,
claim atomique (`UPDATE ... WHERE status='open'`). Empêche que `decide` + un
chemin d'exécution soumettent deux fois.

### 7.2 `external_orders` — machine d'état IB (safety BLOQUANT 1+2)

L'ordre IB est un effet **externe hors transaction SQLite**. On persiste sa
machine d'état :

```
prepared → submitted → (partially_filled) → filled
                     ↘ rejected / cancelled
```

Colonnes clés : `intent_key`, `order_ref` (= `dedup_key`, posé comme `orderRef` IB),
`perm_id`, `client_id`, `order_id`, `status`, `filled_qty`, `avg_fill_px`, horodatages.

**Protocole anti-perte** : on écrit `prepared` **avant** `submit`, puis on met à
jour `submitted`+`perm_id` dès l'accusé IB. Un crash après `submit` mais avant la
mise à jour est rattrapé par la réconciliation (§7.3) via `order_ref`.

### 7.3 Réconciliation IB au boot (safety BLOQUANT 2)

Avant de (re)jouer une intention/ordre, interroger IB : `openOrders` +
`completedOrders`/`executions` + `positions`, corréler par `order_ref` → `perm_id`
→ `order_id`. Traiter les statuts comme une machine d'état :
- `Filled`/exécutions présentes → marquer `filled`, activer le `trade_plan` (§7.4),
  `done`. **Pas de re-submit.**
- `partially_filled` → position modifiée **et** ordre encore ouvert : réconcilier la
  quantité, décider (compléter/annuler) — jamais replay aveugle.
- `PreSubmitted`/`Submitted` → laisser vivre, **ne pas** marquer `done`.
- `Rejected`/`Cancelled` → `failed`/`dead`, pas de replay aveugle.
- Corrélation ambiguë → **halt manuel** (safe default), pas de soumission.

### 7.4 `trade_plan` activé APRÈS fill (arch BLOQUANT + safety)

L'intention porte le futur plan (stop/TP/trailing) en *payload*. Le `trade_plan`
(position ouverte) n'est **créé/activé qu'au fill confirmé**, dans la transaction
qui écrit broker+plan+`done`. Avant fill : rien dans `trade_plans`.

### 7.5 Revalidation avant submit (arch MINEUR 5, safety trous)

`execute_order` (partition `portfolio`, sous `ib=1`) **re-check risk/cash/prix/
session juste avant submit** — les `decide` parallèles ont pu voir un portefeuille
stale (le code actuel fait déjà ces checks avant ordre, `daemon.py:2477/2606`).

### 7.6 Protection résiduelle (safety trous)

Si le daemon tombe **après** l'entrée mais avant d'armer les sorties : prévoir un
stop/TP **broker-side** (ordre protecteur chez IB) ou une reprise qui réarme les
sorties au boot. À trancher dans la revue Lot B.

## 8. Tests (TDD — chaque race → test de non-régression)

Lot A :
| Test | Couvre |
|------|--------|
| 2× enqueue même `dedup_key` → 1 exécution | idempotence |
| 2 tâches même `partition_key` → jamais 2 `running` (+ invariant DB) | sérialisation par clé |
| N workers, `acpx=1`, une sortie prio 0 arrive → servie sans être piégée | resource-aware claim |
| flux continu prio 0-1 → `decide` finit par passer (aging) | anti-famine |
| échec retryable → backoff, `dead` après max | retry |
| `complete()` avec token périmé → refusé | fencing |
| lease expirée au boot → repending ; tâche vivante (heartbeat) → **pas** repending | reprise |
| import migration relancé → aucun doublon (tables non vides → skip) | migration idempotente |
| crash mid-write simulé → état SQLite cohérent | RC-1/2/4 |
| 2ᵉ daemon lancé → refusé (pid-lock) | RC-3 |

Lot B :
| Test | Couvre |
|------|--------|
| crash après `submit` avant maj → réconciliation retrouve l'ordre par `order_ref`, pas de double | external_orders |
| `partial fill` au boot → quantité réconciliée, pas replay aveugle | réconciliation |
| `PreSubmitted` au boot → pas marqué `done` | réconciliation |
| `Rejected` → `failed`, pas de replay | réconciliation |
| intention consommée par un seul exécutant (ancien XOR file) | ownership |
| `trade_plan` absent tant que pas de fill ; présent après fill | plan-après-fill |

Intégration : cycle complet en sim (`SimBroker` + acpx mocké)
`refresh→decide→intention` (Lot A) ; puis `intention→execute→fill→plan` (Lot B).

## 9. Composants (fichiers)

- `trader/queue/ledger.py` — schéma, `enqueue()`, `claim()` (BEGIN IMMEDIATE,
  resource-aware, fencing), `complete()/fail()/retry()`, `recover_on_boot()`.
- `trader/queue/pools.py` — sémaphores de ressource + backpressure AIMD +
  `Condition` de réveil.
- `trader/queue/worker.py` — boucle worker-thread (claim → dispatch → engendre).
- `trader/queue/tasks/` — un handler par `kind` (narrow contract).
- Backends SQLite : `SimBroker`, `TradePlanStore`, `Scheduler` (API inchangée) +
  `schema_migrations`.
- Lot B : `trader/queue/orders.py` (`order_intents`, `external_orders`,
  réconciliation IB).
- Producteur : refonte de la boucle `daemon.py`. Cockpit : vue file.

## 10. Risques & points ouverts

- **Contention SQLite mono-writer** : tenable au volume (~25 symboles, quelques
  writes/s ; seuils de bascule = `SQLITE_BUSY` récurrents, p95 claim > 100-200 ms,
  centaines de writes/s soutenues). Ne jamais tenir de tx pendant l'I/O externe.
- **Corrélation IB** (Lot B) : dépend de la fiabilité `orderRef`/`permId` ; en
  paper (SimBroker) pas d'ordre en vol, mais l'idempotence fill reste requise.
- **Ampleur Phase 1** : migrer 3 stores est le gros du chantier — c'est aussi ce
  qui tue les races critiques.
- **`_LAST_LLM_AT`** volatile (`daemon.py:165`) : à migrer en table (bonus, tue le
  surcoût LLM au reboot).
- **Lot B non implémentable** tant que §7 n'est pas revalidé (Codex).
