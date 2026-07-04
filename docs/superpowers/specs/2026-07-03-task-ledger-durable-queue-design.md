# File de tâches durable in-process (`task_ledger.db`)

**Date** : 2026-07-03
**Statut** : ✅ **LIVRÉ + ACTIVÉ EN PAPER (2026-07-04)** — implémentation complète
(Phases 0-3, Lot A/B outbox) mergée sur main, 3 flags on en paper, migration
validée (`[state-compare] identical=True`). Référence **opérationnelle** :
`docs/reference/task-queue.md`. Ce document reste la **mémoire de conception** (le
POURQUOI : compromis, décisions) — design initial ci-dessous, conservé pour historique.
**Approche retenue** : ① Task-ledger SQLite in-process, threads, migration
*strangler*. Fait-main (zéro dépendance), patterns empruntés au SOTA durable
execution. **Maximise la réutilisation de l'existant** (§3bis).
**Revue** : 2 sessions Codex (axes *architecture/concurrence SQLite* et *safety
trading/migration*), 2026-07-03. Correctifs intégrés. Le verdict STOP safety
portait sur l'exécution *live IB* — or `IBBroker` **n'existe pas** (seul
`SimBroker` tourne, IB n'est qu'une source de données opt-in dormante). Le STOP est
donc reclassé **Lot C (futur)** ; il ne bloque pas le présent (§0).

> **Portée** : remplace le pilotage du daemon (boucle `while-True` synchrone +
> `scheduler.json`) par une **file de tâches durable** adossée à SQLite, et migre
> l'état muté (broker, trade_plans, scheduler) dans le **même substrat** pour
> obtenir des transactions ACID réelles. Objectifs : robustesse au crash,
> reprise/idempotence, voie rapide pour les sorties, isolation par symbole, et
> récupération du débit LLM aujourd'hui bridé.

---

## 0. Découpage en trois lots

Ce qui **tourne aujourd'hui** : broker = **`SimBroker`** (fills simulés synchrones
au dernier prix, état JSON — `execution.py:201`) ; source = **yfinance** par défaut
(`data_source=None`, `daemon.py:2997`) ; IB = source de données **opt-in dormante**
(`IBDataSource`), **aucun** passage d'ordre IB (`IBBroker` est un TODO,
`execution.py:4`).

- **Lot A — Socle file + durabilité de l'état (GO).** File SQLite durcie ;
  migration `broker`/`trade_plans`/`scheduler` en SQLite (tue RC-1/2/4) ;
  `refresh`/`decide` via file ; resource-aware claim ; fencing/lease ; backpressure
  adaptative ; lifecycle acpx. **La file ne passe aucun ordre.**
- **Lot B — Exécution d'ordres via file en PAPER (GO après A).** Avec `SimBroker`,
  `submit()` est **synchrone et déterministe** (fill immédiat, `execution.py:227`) :
  `execute_order` = `broker.submit()` + mutation + `mark done` **dans une seule
  transaction SQLite**. Idempotence gratuite, **aucune réconciliation, pas de STOP**.
- **Lot C — Exécution live IB (FUTUR, gated).** Le jour où un `IBBroker` réel
  (asynchrone) est écrit : machine d'état `external_orders`, corrélation +
  réconciliation IB, `order_intents`, `trade_plan` après fill (§7). **Repoussé,
  non bloquant** ; à revalider par Codex quand l'`IBBroker` existera.

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

## 2. Objectif

File de tâches durable in-process, sans Docker ni service externe :
1. **Robustesse au crash** — état en SQLite (ACID), reprise propre, idempotence.
2. **Voie rapide sorties** — jamais bloquées par un batch LLM.
3. **Isolation par symbole** — un symbole qui plante ne retarde pas les autres.
4. **Débit récupéré** — backpressure adaptative + requeue au lieu de HOLD.

Non-objectifs (YAGNI) : distribution multi-process, broker de messages, workers
externes (§3).

## 3. Décisions (validées)

- **Fait-main SQLite, pas de lib/moteur.** SOTA 2026 : Celery/RQ/Dramatiq/arq/
  taskiq/procrastinate/pgqueuer exigent Redis ou Postgres → écartés (no-Docker).
  Temporal/Restate/Hatchet/Inngest/Windmill → serveur séparé, écartés. **DBOS
  Transact** = seul moteur embarquable (SQLite dev), upgrade transparent si Postgres
  entre en stack ; on emprunte ses patterns sans sa dépendance.
- **Threads in-process, pas d'asyncio.** Justification **corrigée** : ce n'est pas
  « la socket IB » (il n'y a pas d'`IBBroker`) — c'est que le travail est
  **I/O-bound** (subprocess acpx, réseau yfinance), le GIL se relâche, les threads
  donnent tout le parallélisme utile ; le multi-process n'apporterait que
  l'isolation-crash (inutile en Python) au prix d'une grosse complexité. `ib-async`
  (si un jour source IB active) est utilisé en API **synchrone** (`ib_source.py:405`).
- **Migrer l'état dans le même `.db`.** `broker`/`trade_plans`/`scheduler` en tables
  → transactions ACID réelles. Fix de fond RC-1/RC-2/RC-4.
- **`decisions.jsonl` reste** (append-only + `os.replace`, journal d'audit).
- **Multi-process = non** ; `task_ledger` reste le point de découplage si un broker
  REST stateless l'ouvre un jour.

## 3bis. Réutilisation de l'existant (ne rien réinventer — DRY)

On **branche** la file sur les briques déjà en place ; le nouveau code se limite au
`trader/queue/`. Réutilisé tel quel ou derrière la même interface :

| Brique existante | Chemin | Rôle réutilisé |
|---|---|---|
| Pattern SQLite WAL+Lock+busy_timeout | `agent/learnings/store.py:26,31,107` | **modèle direct** du `TaskLedger` (copier le pattern, pas réinventer) |
| Interface `Broker` (Protocol) + `SimBroker` | `execution.py:188,201` | backend SQLite **derrière la même interface** ; l'exécution appelle `broker.submit()` inchangé |
| `TradePlanStore`, `Scheduler` | `trade_plan.py:1010`, `scheduler.py:26` | **API publique identique**, backend SQLite dessous |
| Réveils intelligents (D7/D8) + `scheduler.json` | `scheduler.py`, `daemon.py` | le **producteur lit le scheduler existant** pour savoir quels symboles enfiler |
| `order_admission` (arbitrage gross au mérite) | `trader/application/order_admission` | **réutilisé tel quel** à l'exécution (pas d'arbitrage dans le prompt) |
| `LlmRouter` + classif `retryable` | `llm.py:60-90,144-168` | fallback/backoff LLM **réutilisés** ; **le signal de backpressure = ces classifications** (§4.5) |
| `codex_client.decide_batch` + `context.py` | `client.py:144`, `agent/context.py` | construction prompt + parsing décision **réutilisés** dans le handler `decide` (avec K=1) |
| **Fork acpx** (« bridges jamais orphelins ») | `TRADER_ACPX_BIN` (.env) | **brique de lifecycle** : ferme le bridge en fin d'`exec` (§4.6) |
| `_terminate_process_group` / reap | `llm.py:183-201,268-313` | réutilisé **corrigé** : reap par **PID propre**, pas par cwd (défaut A) |
| `DecisionLedgerStore` (dédup + `os.replace`) | `decision_ledger.py:169,267` | `decisions.jsonl` **inchangé** (journal d'audit) |
| `claim_pid_file` | `pid_file.py:34` | réutilisé **rendu atomique** (`O_EXCL`/`flock`) → RC-3 |
| `measure_d7` | `scripts/measure_d7.py` | **réutilisé** pour l'observation post-bascule |

## 4. Architecture — Lot A (socle)

### 4.1 Modèle d'exécution & concurrence

Un process. Le `while-True` devient un **producteur** : lit le scheduler (table),
enfile des tâches, dort. Un **pool de N worker-threads** consomme le ledger.

Trois mécanismes superposés :
1. **Pools de ressources** (sémaphores nommés) : `acpx` (limite **adaptative**,
   §4.5), `yahoo` (basse, anti-429), `portfolio` (**1** = verrou de mutation de
   portefeuille — sérialise cash/positions, `SimBroker` aujourd'hui, IB demain).
   **Une seule ressource par tâche** — pas d'acquisition imbriquée (évite tout
   deadlock ; correctif arch MAJEUR 4).
2. **Sérialisation par clé (`partition_key`)** : au plus une tâche `running` par
   clé (symbole, ou `"portfolio"`). Imposée au claim ET par un invariant DB.
3. **GIL non bloquant** (I/O-bound).

### 4.2 Schéma `task_ledger.db`

WAL + `busy_timeout=5000` + `check_same_thread=False` + `threading.Lock` (pattern
`agent/learnings/store.py`). **Timestamps en epoch integer (ms UTC)** — jamais de TEXT
comparé lexicographiquement (correctif arch, trous).

```sql
CREATE TABLE tasks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT NOT NULL,
  dedup_key      TEXT UNIQUE,          -- idempotency key
  partition_key  TEXT,                 -- "AAPL" | "portfolio" | NULL
  resource       TEXT,                 -- acpx | yahoo | portfolio | NULL (UNE seule)
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

**Claim atomique** — en transaction `BEGIN IMMEDIATE`, **resource-aware** (ne claim
que si un permit de la ressource est libre, sinon les workers se piègent tous sur
`acpx` et la voie rapide meurt) :

```sql
BEGIN IMMEDIATE;
UPDATE tasks SET status='running', claimed_by=:w, claim_token=:token,
                 lease_expires_at=:lease, attempts=attempts+1, updated_at=:now
WHERE id = (
  SELECT id FROM tasks
  WHERE status='pending' AND scheduled_at <= :now
    AND attempts < max_attempts
    AND (resource IS NULL OR resource IN (:free_resources))
    AND (partition_key IS NULL
         OR partition_key NOT IN (SELECT partition_key FROM tasks
                                   WHERE status='running' AND partition_key IS NOT NULL))
  ORDER BY priority ASC, scheduled_at ASC, id ASC
  LIMIT 1)
RETURNING *;
COMMIT;
```

Le worker consomme/ferme le curseur avant `COMMIT`, gère `SQLITE_BUSY`, n'utilise
pas `rowcount`. `complete()/fail()` gardés par le fencing token :
`UPDATE ... WHERE id=:id AND claim_token=:token`.

**Réveil sur release** : `Condition.wait()`/notify sur libération de permit, pas un
simple polling 200 ms. Le polling reste le filet.

**Jamais de transaction SQLite ouverte pendant un appel acpx/yahoo/IB** : on claim
(tx courte), on relâche, on fait l'I/O, on réécrit (tx courte).

### 4.3 Taxonomie des tâches, priorités

| `kind` | priorité | resource | partition | engendre |
|--------|----------|----------|-----------|----------|
| `apply_exits` | **0 (EXIT)** | portfolio | portfolio | — |
| `scan_watches` | 0 | — | — | intention |
| `refresh_symbol` | 5 | yahoo¹ | symbol | `decide` |
| `decide` | 5 (DECISION) | acpx | symbol² | **intention** |
| `arm_watch` | 5 | — | symbol | — |
| `execute_order` | **1 (EXEC)** | portfolio | portfolio | — |
| `consolidate_learnings` | **9 (MAINT)** | acpx | — | — |

¹ `yahoo` = source active (défaut) ; `ib` si source IB opt-in un jour (une seule
ressource data). ² grain-symbole par défaut — voir §4.3bis.

### 4.3bis Granularité de `decide` : grain-symbole par défaut (décision 2026-07-03)

**1 tâche `decide` = 1 symbole = 1 appel acpx** (`partition_key = symbole`). On
écarte le chunking par défaut du batch actuel (~5 symboles/prompt), après analyse :

- La **vue comparative** qu'apportait le chunk est **redondante** : l'agent a déjà
  son état portefeuille dans le contexte (`equity_usd`, `context.py:119` ; `cash`/
  positions, `daemon.py:218`), et l'arbitrage de marge entre candidats est fait **par
  le code** (`order_admission`, au mérite/conviction, déterministe), pas par le LLM.
- Le **coût en nombre d'appels** est atténué par les **réveils intelligents** : on ne
  décide que les symboles *dus* (souvent 1-3/tick), pas tout l'univers.
- Le seul surcoût réel est la **redondance du contexte de base** (mandat/régime/
  portefeuille répétés par appel) — écrasée par le **prompt caching** (préfixe commun
  cachable ; seul le bloc symbole varie).

Bénéfices : `partition_key = symbole` **directement** (pas de « chunk-id » ; l'index
`uniq_active_kind_partition` sur `(decide, symbole)` empêche nativement deux `decide`
du même symbole) ; **isolation native par symbole** ; prompt focalisé ; un pic (open
de marché) s'écoule par les M guichets à la capacité de l'app-server.

**Échappatoire** : la taille de regroupement `K` reste **configurable** (défaut
`K=1` = grain-symbole). `K>1` restaure un chunk si le coût token remontait — YAGNI.

**Modèle d'exécution des appels acpx** : le pool `acpx` = **M « guichets »**
(M = limite adaptative, §4.5). Jusqu'à **M appels en parallèle**, un par symbole ; le
reste attend, servi dès qu'un guichet se libère (priorité aux sorties).

**Voie rapide** : `ORDER BY priority ASC` sert EXIT/EXEC avant DECISION.
**Anti-famine** (correctif arch 6 + safety 7) : *aging* — au-delà d'un seuil
d'attente (`enqueued_seq`), une tâche `decide` gagne en priorité effective ; et on
**ne lance pas** de nouvelle entrée tant qu'une sortie du même portefeuille est
`pending`.

### 4.4 `decide` produit une INTENTION ; l'exécution est simple en paper

Le `trade_plan` représente une **position ouverte** (créé *après* fill aujourd'hui,
`daemon.py:2628/2706`). Donc :

- `decide` → jeton `acpx` → LLM → écrit une **décision d'audit** et, si trade, une
  **intention** `pending`. **Aucun plan, aucun ordre dans `decide`.**
- `execute_order` (Lot B paper, `resource=portfolio`) consomme l'intention et, comme
  `SimBroker.submit()` est **synchrone/déterministe**, écrit **broker + plan +
  `done` dans UNE transaction** (revalidation risk/cash/prix avant, réutilise
  `order_admission` + les checks `daemon.py:2477/2606`). Idempotence gratuite.
- **Live IB** (Lot C) : l'ordre devient un effet externe asynchrone → machine d'état
  `external_orders` + réconciliation (§7). *Non implémenté tant qu'`IBBroker`
  n'existe pas.*

### 4.5 Erreurs, retry, backpressure, reprise

**Deux gardes de temps DISTINCTES — ne pas les confondre** :

- **Heartbeat + lease → panne « le daemon entier meurt ».** Un thread dédié prolonge
  `lease_expires_at` des tâches `running` tant que le daemon vit. S'il meurt, au boot
  `recover_on_boot()` repasse en `pending` les `running` à lease expirée. **Un agent
  qui réfléchit longtemps n'est jamais orphelin tant que le daemon vit** (heartbeat
  dans un thread séparé du worker bloqué sur l'I/O).
- **Timeout du subprocess acpx → panne « un appel se fige »** (bridge zombie). Le
  heartbeat ne peut PAS le casser (worker vivant, bloqué dans `communicate()`,
  tiendrait le jeton `acpx` à vie). Timeout subprocess **desserré + configurable par
  `kind`** (ex. `decide` 30-60 min) ; dépassement = **`requeue` (retryable), jamais
  `HOLD`**. *(Futur : idle-watchdog sur inactivité quand acpx streamera ;
  `--format quiet` + `communicate()` bloquant aujourd'hui, `llm.py:106,299`.)*

- **Retry + backoff** : échec *retryable* → `pending`, `scheduled_at = now +
  base·2^attempts`, jusqu'à `max_attempts` → `dead`. `decide` idempotent → rejeu sûr
  → **fin du HOLD-par-saturation**.
- **Backpressure adaptative `acpx` = contrôleur de concurrence à DOUBLE SIGNAL**
  (réutilise la classif d'erreurs `llm.py:144-168`) :
  - *Signal dur (réactif)* : un `LlmFailure` overload / internal-error / sortie vide
    → **decrease agressif** `M ×0.5`.
  - *Signal doux (proactif)* : latence glissante (p50/p95) des appels acpx ; si elle
    gonfle anormalement → **gel de l'increase** (pré-choke détecté avant les erreurs).
  - *Increase prudent* : série saine → `M +1`, jusqu'à un **plafond configurable**.
  Pattern *adaptive concurrency limiting* (AIMD ; raffinable en Gradient/Vegas si
  oscillation). Couplé au **claim resource-aware** (§4.2). `M` trouve seul le point
  où acpx choke et s'y tient.
- **Fencing token** : `complete()/fail()` gardés par `claim_token`.
- **pid-lock atomique** (`O_EXCL`/`fcntl.flock`) au boot → résout RC-3.

### 4.6 Lifecycle des appels acpx (même cadran que la backpressure)

En grain-symbole, **1 `decide` = 1 `acpx exec` one-shot = 1 bridge `codex-acp`
éphémère**. Donc **`M` appels concurrents = `M` bridges vivants au même instant** :
le `M` calculé par la backpressure (§4.5) est *aussi* le plafond de bridges
simultanés → **contrôler `M` protège l'app-server ET borne le pileup d'un seul
geste**. Le lifecycle par appel réutilise l'existant :

- **Fork acpx en service** (`TRADER_ACPX_BIN`, « bridges jamais orphelins ») : ferme
  le bridge en fin d'`exec` — brique de lifecycle propre déjà validée.
- **Worker en filet** : garantit la fermeture du groupe de process, **reap par PID
  propre** (celui de *son* subprocess), **jamais par cwd partagé** — tue le
  reap-croisé (défaut A) qui, en grain-symbole parallèle, reviendrait sinon.
  Réutilise `_terminate_process_group` (`llm.py:183-201`) corrigé.
- **Reap de sécurité périodique** : un balayage des orphelins résiduels (dernier
  filet), tracé.

## 5. Migration des stores d'état (Lot A) — idempotente

Backends SQLite pour `SimBroker` / `TradePlanStore` / `Scheduler`, **derrière l'API
publique existante** (Protocol `Broker`, `execution.py:188` ; consommateurs
inchangés). Migration one-shot au boot, **idempotente** (correctif safety MAJEUR 4) :

- Table `schema_migrations` (version appliquée).
- Import JSON → tables **seulement si les tables sont vides** ; sinon skip.
- Contraintes `UNIQUE` (un fill par `dedup_key`, un plan par `id`) → bloque tout
  doublon même si l'import est relancé.
- Backup horodaté des `.json` avant bascule.
- **SimBroker** : mutation d'état **et** `mark done` dans **une seule transaction**
  (correctif safety MAJEUR 3) → pas de double mutation au rejeu, même en paper.

## 6. Migration *strangler* (réversible par flag)

- **Phase 0 — Substrat débranché.** Module `queue/`, `task_ledger.db`, tests. Flag
  maître `CASYS_QUEUE_ENABLED=0`.
- **Phase 1 — État en SQLite, comportement inchangé** (§5). Mono-thread. →
  **RC-1/2/4 morts ici.**
- **Phase 2 — File en shadow.** Producteur enfile `refresh_symbol`+`decide`, on
  *compare* décisions/intentions file vs chemin synchrone (équivalence, risque nul).
- **Phase 3 (Lot A complet) — `refresh`/`decide` via file** ; exécution encore
  synchrone.
- **Phase 4 (Lot B paper) — `execute_order` via file avec `SimBroker`** (transaction
  unique, §4.4). Un flag, ancien chemin en fallback. **Simple, pas de STOP.**
- **Phase 5 (Lot C, futur) — live IB** : seulement quand `IBBroker` existe (§7).
- **Phase 6 — Nettoyage** après observation (`measure_d7`).

**Réversibilité** : par flag pour les classes de tâches ; pour le substrat état,
« rollback » = *SQLite backend + queue off*, PAS un retour au JSON (backup stale).
Export JSON atomique/versionné fourni comme filet.

**Observabilité** : vue cockpit `pending/running/dead/retry` par `kind`.

## 7. Lot C — Exécution LIVE IB (FUTUR — à revalider Codex quand `IBBroker` existera)

> **Ne s'implémente pas maintenant.** `IBBroker` n'existe pas ; en paper (SimBroker)
> l'exécution est synchrone et simple (§4.4). Cette section capture le design de
> sûreté à activer **le jour où** un `IBBroker` asynchrone sera écrit — pour ne pas
> reperdre les findings Codex safety.

### 7.1 `order_intents` — ownership exclusif (safety BLOQUANT 5)

Table unique consommée par **exactement un** exécutant (ancien chemin OU file,
jamais les deux). `UNIQUE(intent_key)`, statut `open/claimed/executed/cancelled`,
claim atomique. Empêche la double soumission pendant une bascule.

### 7.2 `external_orders` — machine d'état IB (safety BLOQUANT 1+2)

L'ordre IB est un effet **externe hors transaction SQLite**. Machine d'état :
`prepared → submitted → (partially_filled) → filled` / `rejected|cancelled`.
Colonnes : `intent_key`, `order_ref` (=`dedup_key`, posé comme `orderRef` IB),
`perm_id`, `client_id`, `order_id`, `status`, `filled_qty`, `avg_fill_px`. Écrire
`prepared` **avant** `submit` ; crash entre les deux rattrapé par §7.3 via `order_ref`.

### 7.3 Réconciliation IB au boot (safety BLOQUANT 2)

Interroger `openOrders` + `completedOrders`/`executions` + `positions`, corréler par
`order_ref`→`perm_id`→`order_id`. `Filled` → activer plan + `done` ;
`partially_filled` → réconcilier quantité ; `PreSubmitted`/`Submitted` → laisser
vivre ; `Rejected`/`Cancelled` → `failed`/`dead` ; ambigu → **halt manuel**.

### 7.4 `trade_plan` activé APRÈS fill · 7.5 revalidation avant submit · 7.6 stop/TP broker-side

Plan (position ouverte) créé **au fill confirmé** seulement. Re-check
risk/cash/prix/session avant submit (`order_admission` + `daemon.py:2477/2606`). Si
le daemon tombe après entrée : stop/TP **broker-side** ou réarmement au boot.

## 8. Tests (TDD — chaque race → test de non-régression)

**Lot A** :
| Test | Couvre |
|------|--------|
| 2× enqueue même `dedup_key` → 1 exécution | idempotence |
| 2 tâches même `partition_key` → jamais 2 `running` (+ invariant DB) | sérialisation |
| N workers, `acpx` saturé, sortie prio 0 → servie sans piège | resource-aware |
| flux continu prio 0-1 → `decide` finit par passer (aging) | anti-famine |
| échec retryable → backoff, `dead` après max | retry |
| overload → `M ×0.5` ; latence haute → increase gelé ; sain → `M +1` | backpressure double-signal |
| `M` bridges max vivants ; reap par PID propre ne tue pas le voisin | lifecycle acpx / défaut A |
| timeout acpx desserré dépassé → `requeue`, jamais `HOLD` | garde acpx |
| worker bloqué mais daemon vivant (heartbeat) → tâche **pas** reprise | garde daemon vs acpx |
| `complete()` token périmé → refusé | fencing |
| lease expirée au boot → repending ; heartbeat récent → **pas** repending | reprise |
| import migration relancé → aucun doublon | migration idempotente |
| crash mid-write simulé → état SQLite cohérent | RC-1/2/4 |
| 2ᵉ daemon lancé → refusé (pid-lock) | RC-3 |

**Lot B (paper)** :
| Test | Couvre |
|------|--------|
| `execute_order` : `submit`+mutation+`done` atomiques ; rejeu → pas de double mutation | idempotence paper |
| intention consommée par un seul exécutant (ancien XOR file) | ownership |
| `trade_plan` absent tant que pas de fill ; présent après fill | plan-après-fill |

**Lot C (futur, à écrire avec l'`IBBroker`)** : crash après `submit` → réconciliation
par `order_ref` ; partial fill ; `PreSubmitted` non `done` ; `Rejected` → `failed`.

Intégration : cycle en sim (`SimBroker` + acpx mocké)
`refresh→decide→intention→execute→fill→plan`.

## 9. Composants (fichiers)

**Nouveau** (`trader/queue/`) :
- `ledger.py` — schéma, `enqueue`, `claim` (BEGIN IMMEDIATE, resource-aware,
  fencing), `complete/fail/retry`, `heartbeat`, `recover_on_boot`.
- `pools.py` — sémaphores + backpressure double-signal + `Condition`.
- `worker.py` — boucle worker-thread + lifecycle acpx (reap PID propre) + thread heartbeat.
- `tasks/` — un handler par `kind` (réutilise `decide_batch`/`order_admission`/`SimBroker`).

**Modifié / réutilisé** : `SimBroker`/`TradePlanStore`/`Scheduler` (backend SQLite,
API inchangée) + `schema_migrations` ; `pid_file` (atomique) ; producteur =
refonte boucle `daemon.py` ; cockpit (vue file). **Lot C** : `trader/queue/orders.py`.

## 10. Risques & points ouverts

- **Contention SQLite mono-writer** : tenable au volume (~25 symboles, quelques
  writes/s ; bascule = `SQLITE_BUSY` récurrents, p95 claim > 100-200 ms). Jamais de
  tx pendant l'I/O externe.
- **Ampleur Phase 1** : migrer 3 stores est le gros du chantier — et ce qui tue les
  races critiques.
- **`_LAST_LLM_AT`** volatile (`daemon.py:165`) : à migrer en table (bonus).
- **Lot C (live IB)** : non implémentable tant qu'`IBBroker` n'existe pas ; §7 à
  revalider Codex à ce moment-là.
