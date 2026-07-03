# Migration de l'état vers SQLite — Phase 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrer l'état persisté du daemon (`broker.json`, `trade_plans.json`, `scheduler.json`) de fichiers JSON non-atomiques vers un substrat **SQLite unique** (`state/casys.db`, WAL), derrière l'API publique **inchangée** des 3 stores — pour obtenir des écritures atomiques et des transactions ACID (tue RC-1/2/4, prépare l'outbox du Lot B).

**Architecture:** Un module `trader/state_db/` fournit une connexion SQLite partagée (WAL + Lock, pattern `learnings/store.py`) et un registre de migrations. Chaque store (`SimBroker`, `TradePlanStore`, `Scheduler`) gagne un backend SQLite **derrière sa signature publique actuelle**. Transition dé-risquée par **double-write shadow** : SQLite = source de vérité, mais un miroir JSON **atomique** (`tmp+os.replace`) reste écrit pour les nombreux **lecteurs hors-store** (cockpit, TUI, CLI, rotation, read_models) jusqu'à leur migration en §1e. Migration one-shot **idempotente** au boot (import JSON → tables si vides, `schema_migrations`, backup).

**Tech Stack:** Python stdlib (`sqlite3`, `threading`, `json`, `os`), `pytest`. Zéro dépendance nouvelle.

## Global Constraints

- **Zéro dépendance nouvelle** ; `sqlite3`/`threading` stdlib.
- **Fichier unique** `state/casys.db` (partagé avec le futur `task_ledger` du Lot B, pour l'outbox). WAL + `busy_timeout=5000` + `check_same_thread=False` + `threading.Lock` (pattern `trader/learnings/store.py:26,31,107`).
- **API publique des 3 stores INCHANGÉE** — aucun appelant *via l'objet store* ne doit être modifié. Vérifié par les tests existants qui doivent rester verts sans édition.
- **Double-write shadow JSON atomique** (`tmp` + `os.replace`) tant que les lecteurs hors-store (§1e) ne sont pas migrés. Le JSON est un **miroir** ; SQLite fait foi.
- **Migration idempotente** : `schema_migrations` (version) ; import JSON→tables **seulement si les tables sont vides** ; backup horodaté du `.json` avant bascule ; contraintes `UNIQUE` bloquant tout doublon si l'import est relancé.
- **Temps** : conserver le format existant de chaque store (SimBroker/TradePlan : ISO strings `TEXT` tels quels ; Scheduler : ISO strings `TEXT`). Ne PAS convertir en epoch (les lecteurs JSON attendent l'ISO). Cohérence > micro-optim.
- **Déterminisme AX** : le temps reste injecté (`now`) où les stores le prennent déjà (Scheduler). Pas de `datetime.now()` caché ajouté.
- **Vérifier le vrai exit code pytest** : `uv run pytest …; echo "EXIT=$?"` — jamais conclure vert sur un pipe.
- **Ordre imposé** : §1a → §1b → §1c → §1d → §1e. Un flag `CASYS_STATE_BACKEND` (`json`|`sqlite`, défaut `json`) permet de basculer store par store et de **revenir en arrière** tant que §1e n'est pas fait.

---

## Contexte : ce qui existe (cartographié 2026-07-03)

**Les 3 stores et leur API publique** (à préserver à l'identique) :

- `SimBroker` (`trader/tools/execution.py:201`) implémente le Protocol `Broker` (`:188`) : `submit(order, price, ts, dry_run, fx_rate) -> Fill|None`, `positions() -> dict[str,Position]`, `cash() -> float`. État `_State(cash: float, positions: dict[str,dict], fills: list[dict])`.
- `TradePlanStore` (`trader/planning/trade_plan.py:1010`) : `open_plans()`, `upsert(plan)`, `close(id)`, `close_symbol(symbol)`, `sync_symbol_quantity(symbol, remaining_quantity)`, `clear()`. `TradePlan` = dataclass frozen ~24 champs (PK `id = {symbol}-{opened_at}`), sous-objets `TakeProfit`/`TrailingStop`/`ProfitProtection` + dicts bruts `exit_watch`/`last_llm_review`/`entry_context`.
- `Scheduler` (`trader/tools/scheduler.py:26`) — **17 méthodes** : `set_default_next_wake`/`set_next_wake`/`set_next_wake_in`, `set_symbol_next_wake`/`clear_symbol_next_wake`/`set_symbol_next_wake_in`, `symbols_with_wake`, `has_symbol_wake`, `next_wake`, `due_symbols`, `seconds_until_wake`, `set_symbol_indicator_watch`, `active_indicator_watches`, `pop_expired_indicator_watches`, `remove_indicator_watch`, `reconcile_universe`, `get_stale_streak`/`set_stale_streak`/`reset_stale_streak`. État : `default_next_wake` (ISO|null), `symbols` (dict sym→ISO), `stale_streaks` (dict sym→int), `indicator_watches` (dict id→watch, `watch.conditions` = liste, `watch.order` imbriqué si `EXECUTE_ORDER`).

**Instanciations (production)** : `daemon.py:1722` (broker), `daemon.py:1727` (plans), `daemon.py:3110` (scheduler), `STATE_DIR = ROOT/state`.

**⚠️ Lecteurs HORS-STORE (lisent les JSON directement — le vrai coût de la migration)** :

| JSON | Lecteurs directs |
|---|---|
| `broker.json` | `read_models/runtime_state.py:294` (`_load_fills_safe`), `reporting/stats.py:136`, `rotation/collectors.py:69`, `cockpit/app.py` |
| `trade_plans.json` | `read_models/runtime_state.py:141`, `runtime/cli.py:171` |
| `scheduler.json` | `read_models/runtime_state.py:184,462`, `runtime/cli.py:170`, `ui/tui.py:36` |

Ces lecteurs sont **fail-safe** (try/except → `[]`/`{}`). Tant qu'on écrit le **shadow JSON** (§1a-1d), ils continuent de marcher inchangés ; §1e les migre vers SQLite et retire le shadow.

**Instanciations parasites à neutraliser** : `rotation/collectors.py:71` (`SimBroker(...)`) et `:97` (`TradePlanStore(...)`) créent le fichier s'il est absent — en SQLite, l'ouverture ne doit PAS créer d'état parasite (mode lecture seule / ne pas initialiser).

---

## File Structure

- Create `trader/state_db/__init__.py` — package.
- Create `trader/state_db/connection.py` — `StateDb` : connexion SQLite partagée (WAL, Lock), `apply_migrations`, `transaction()` context manager, `table_is_empty(name)`.
- Create `trader/state_db/shadow.py` — `write_json_atomic(path, data)` (`tmp`+`os.replace`) + helper de double-write.
- Create `trader/state_db/migrations.py` — schéma des tables (broker/plans/scheduler) + `schema_migrations`, import one-shot idempotent depuis JSON.
- Modify `trader/tools/execution.py` — `SimBroker` : backend SQLite optionnel (flag), double-write shadow.
- Modify `trader/planning/trade_plan.py` — `TradePlanStore` : idem.
- Modify `trader/tools/scheduler.py` — `Scheduler` : idem.
- Modify (§1e) `read_models/runtime_state.py`, `reporting/stats.py`, `rotation/collectors.py`, `runtime/cli.py`, `ui/tui.py`, `cockpit/app.py` — lire SQLite au lieu du JSON, puis retrait du shadow.
- Modify `runtime/daemon.py` — sélection du backend via `CASYS_STATE_BACKEND`, migration au boot.
- Tests : `tests/state_db/` (nouveau — attention au nom, ne PAS nommer un dossier `queue`/`state` qui masque un stdlib ; `state_db` est sûr).

---

## §1a — Substrat `state_db` (connexion + migrations + shadow)

**Objectif** : le socle commun, testé, sans toucher aux stores. Aucun impact runtime (rien ne l'utilise encore).

### Task 1 — `StateDb` : connexion partagée WAL + transaction

**Files:** Create `trader/state_db/__init__.py` (vide), `trader/state_db/connection.py` ; Test `tests/state_db/__init__.py` (vide), `tests/state_db/test_connection.py`.

**Interfaces — Produces:**
- `StateDb(db_path)` : ouvre `sqlite3.connect(check_same_thread=False)`, `row_factory=Row`, `PRAGMA journal_mode=WAL`, `busy_timeout=5000`. Attribut `path`, `_lock` (threading.Lock).
- `StateDb.transaction()` : context manager `BEGIN IMMEDIATE` … `COMMIT` / `rollback` sur exception, sous `_lock`.
- `StateDb.executescript(sql)` / `execute(sql, params)` gardés par `_lock`.
- `StateDb.table_is_empty(name) -> bool`.

- [ ] **Step 1: failing test** — `test_wal_and_transaction_atomicity` : ouvrir un `StateDb` sur `tmp_path`, vérifier `PRAGMA journal_mode == 'wal'` ; dans une `transaction()`, faire un INSERT puis lever → vérifier rollback (table vide). Deuxième `transaction()` qui commit → ligne présente.
- [ ] **Step 2: run, expect fail** (`ModuleNotFoundError`).
- [ ] **Step 3: implement** `connection.py` (voir Interfaces ; `transaction()` = `try: cur.execute("BEGIN IMMEDIATE"); yield cur; conn.commit() except: conn.rollback(); raise`, le tout `with self._lock`).
- [ ] **Step 4: run, expect pass.**
- [ ] **Step 5: commit** `feat(state_db): StateDb connexion WAL partagée + transaction atomique`.

### Task 2 — `schema_migrations` + `apply_migrations`

**Files:** Modify `connection.py` (ou Create `migrations.py`) ; Test `tests/state_db/test_migrations.py`.

**Interfaces — Produces:** `StateDb.apply_migrations(migrations: list[tuple[int, str]])` : crée `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)`, applique chaque migration dont la `version` n'est pas déjà enregistrée, dans une transaction, idempotent (re-run = no-op).

- [ ] **Step 1: failing test** — appliquer 2 migrations, vérifier tables créées + `schema_migrations` a 2 lignes ; ré-appliquer → toujours 2 lignes, pas d'erreur.
- [ ] **Steps 2-4** (TDD).
- [ ] **Step 5: commit** `feat(state_db): schema_migrations idempotent`.

### Task 3 — `write_json_atomic` (shadow) + helper double-write

**Files:** Create `trader/state_db/shadow.py` ; Test `tests/state_db/test_shadow.py`.

**Interfaces — Produces:** `write_json_atomic(path: Path, data: dict) -> None` : écrit `tmp` (`path.with_suffix('.tmp')`) puis `os.replace(tmp, path)` (atomique) ; `json.dumps(indent=2, ensure_ascii=False)` pour matcher l'existant.

- [ ] **Step 1: failing test** — `write_json_atomic` produit un JSON relisible identique à `write_text(json.dumps(...))` ; simuler un crash (écrire un tmp puis ne pas replace) → le fichier cible n'est jamais partiellement écrit (pas de `.tmp` visible comme cible).
- [ ] **Steps 2-4.**
- [ ] **Step 5: commit** `feat(state_db): write_json_atomic (shadow JSON tmp+os.replace)`.

**Point de validation Codex #1** : review `state_db/` (transaction, migrations idempotentes, atomicité shadow) avant d'attaquer les stores.

---

## §1b — `SimBroker` sur SQLite (le plus simple, POC de l'approche)

**Objectif** : valider le pattern (backend SQLite + double-write + migration + flag) sur le store le plus petit avant les gros.

### Schéma (migration v1)
```sql
CREATE TABLE broker_state (id INTEGER PRIMARY KEY CHECK(id=1), cash REAL NOT NULL);
CREATE TABLE broker_positions (
  symbol TEXT PRIMARY KEY, quantity REAL NOT NULL, avg_price REAL NOT NULL);
CREATE TABLE broker_fills (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT, side TEXT, quantity REAL, price REAL, ts TEXT,
  commission REAL DEFAULT 0.0, commission_currency TEXT DEFAULT 'USD',
  commission_model TEXT DEFAULT 'none', fx_rate REAL DEFAULT 1.0);
```

### Invariants de non-régression (issus de la cartographie — À NE PAS CASSER)
- `submit(dry_run=True)` ne mute rien, retourne `None`.
- `submit(dry_run=False)` : mute position + cash + append fill **dans UNE transaction** (`StateDb.transaction()`), retourne le `Fill`. La logique d'`avg_price` (long/short/retournement, `execution.py:248-254`) est **portée telle quelle en Python** (pas déléguée à SQL).
- **Positions `quantity==0` conservées** en table (historique) ; `positions()` filtre `WHERE quantity != 0` — comme `execution.py:265`.
- `cash()` = `SELECT cash FROM broker_state`.
- **Fills hétérogènes** : la migration one-shot depuis `broker.json` patche les champs manquants (`commission=0.0`, `commission_currency='USD'`, `commission_model='none'`, `fx_rate=1.0`) sur les anciens fills.
- **Double-write shadow** : après chaque `submit` live, ré-écrire `broker.json` atomiquement (miroir `{cash, positions, fills}` reconstruit depuis les tables, format identique) pour les lecteurs hors-store.
- **Instanciation parasite** (`collectors.py:71`) : le backend SQLite ne doit PAS créer d'état si le fichier n'existe pas en mode lecture (paramètre `create=False` ou ouverture read-only).

### Tâches (TDD)
- [ ] **Task 4** — migration v1 broker + `import_broker_from_json(db, json_path)` idempotent (import si `broker_state` vide ; patch fills ; backup `broker.json.bak-<ts>`). Test : import depuis un `broker.json` fixture (avec fills hétérogènes + positions q=0) → tables peuplées, defaults patchés ; ré-import → no-op.
- [ ] **Task 5** — `SimBroker` backend SQLite derrière l'API : `cash()`, `positions()` (filtre q≠0). Flag `backend='sqlite'`. Test : mêmes retours que le backend JSON sur le même état.
- [ ] **Task 6** — `submit()` transactionnel (position+cash+fill en une transaction) + logique avg_price portée. Test : reproduire les cas de `test_execution.py` (open long, add, reduce, reverse, short) → mêmes cash/positions/fills que le backend JSON. Test crash : exception au milieu de submit → rollback, état inchangé.
- [ ] **Task 7** — double-write shadow `broker.json` après submit. Test : après un submit SQLite, `broker.json` relu = miroir exact des tables (cash/positions/fills).
- [ ] **Task 8** — câblage `daemon.py` derrière `CASYS_STATE_BACKEND=sqlite` (défaut `json`) + migration au boot. Test : le daemon en mode sqlite tourne un cycle sim sans régression (réutiliser un test d'intégration existant en forçant le flag).

**Point de validation Codex #2** : review SimBroker SQLite (transactionnalité submit, avg_price, parité JSON/SQLite, shadow) — c'est le POC, on valide le pattern avant de le répliquer.

---

## §1c — `TradePlanStore` sur SQLite

### Schéma (migration v2)
```sql
CREATE TABLE trade_plans (
  id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT, quantity REAL,
  remaining_quantity REAL, entry_price REAL, opened_at TEXT,
  reference_volatility REAL, hard_stop_price REAL, max_hold_minutes REAL,
  high_watermark REAL, low_watermark REAL,
  -- sous-objets scalaires aplatis :
  trailing_json TEXT,            -- TrailingStop (petit, JSON)
  profit_protection_json TEXT,   -- ProfitProtection (JSON)
  take_profits_json TEXT,        -- list[TakeProfit] (JSON — modifiée en bloc par sync_symbol_quantity)
  filled_take_profits_json TEXT, -- list[str] (JSON)
  exit_watch_json TEXT, last_llm_review_json TEXT, entry_context_json TEXT,
  llm_provider TEXT, llm_model TEXT, llm_fallback_reason TEXT,
  llm_confidence REAL, entry_thesis TEXT, entry_decision_id TEXT);
CREATE INDEX idx_trade_plans_symbol ON trade_plans(symbol);
```
> Choix : colonnes scalaires en dur (requêtables), sous-objets/listes en **JSON colonnes** (`asdict`/`json.dumps` au write, `trade_plan_from_dict` au read). Justif : `sync_symbol_quantity` recalcule les quantités de la **liste entière** de TP → une table jointe n'apporte rien ici, le JSON est plus simple et fidèle à `asdict`.

### Invariants de non-régression
- `upsert(plan)` = `INSERT OR REPLACE` sur `id` (unicité par id, `trade_plan.py:1031`).
- `open_plans()` = `SELECT *` → `trade_plan_from_dict` (round-trip exact via `asdict`/`from_dict`, y compris champs `None` et non-finis tolérés).
- `close(id)`, `close_symbol(symbol)` = `DELETE`.
- `sync_symbol_quantity(symbol, remaining)` : si `remaining<=0` → `close_symbol` ; sinon recalcul ratio sur les plans du symbole (logique `trade_plan.py:1045+` portée), **close+update dans UNE transaction**.
- **Plusieurs plans par symbole** possibles (cas ADD) — pas de contrainte unique sur `symbol`.
- Double-write shadow `trade_plans.json` (`{"plans": [asdict(p) …]}`).

### Tâches (TDD)
- [ ] **Task 9** — migration v2 + `import_trade_plans_from_json` idempotent (backup). Test : import fixture (plan complet avec take_profits/exit_watch/entry_context) → round-trip `open_plans()` identique à l'original.
- [ ] **Task 10** — `TradePlanStore` backend SQLite : `open_plans`, `upsert`, `close`, `close_symbol`, `clear`. Test : parité avec backend JSON (réutiliser les tests existants de `trade_plan` en forçant le backend).
- [ ] **Task 11** — `sync_symbol_quantity` transactionnel (close+update en une tx). Test : cas remaining<=0 (close_symbol), cas partiel (ratio + TP conservés).
- [ ] **Task 12** — double-write shadow + câblage `daemon.py:1727`. Test : `trade_plans.json` miroir exact ; cycle daemon sim sans régression.

**Point de validation Codex #3** : review TradePlanStore (round-trip des champs complexes, sync_symbol_quantity transactionnel, parité).

---

## §1d — `Scheduler` sur SQLite (le gros — 17 méthodes)

### Schéma (migration v3)
```sql
CREATE TABLE scheduler_meta (key TEXT PRIMARY KEY, value TEXT);   -- default_next_wake
CREATE TABLE scheduler_symbol_wake (symbol TEXT PRIMARY KEY, when_iso TEXT NOT NULL);
CREATE TABLE scheduler_stale_streaks (symbol TEXT PRIMARY KEY, streak INTEGER NOT NULL);
CREATE TABLE scheduler_watches (
  id TEXT PRIMARY KEY, symbol TEXT NOT NULL, created_at TEXT, expires_at TEXT,
  on_trigger TEXT, watch_json TEXT NOT NULL);   -- watch complet (conditions+order) en JSON
CREATE INDEX idx_watches_symbol ON scheduler_watches(symbol);
CREATE INDEX idx_watches_expires ON scheduler_watches(expires_at);
```
> Choix : `watch_json` stocke le dict complet (conditions liste + order imbriqué) — évite 2 tables jointes pour une structure semi-ouverte ; `symbol`/`expires_at`/`on_trigger` extraits en colonnes pour les requêtes (`active`/`pop_expired`/`reconcile`).

### Invariants de non-régression (les plus délicats — 17 méthodes)
- **Migration legacy** : la clé `next_wake` (ancienne) est promue `default_next_wake` à l'import (`scheduler.py:34`).
- `default_next_wake`/`symbols`/`stale_streaks` : CRUD direct par clé.
- `due_symbols` : aujourd'hui 1 load/symbole ; en SQL, `next_wake(symbol)` = `SELECT` ciblé (gain).
- `set_symbol_indicator_watch` : **coexistence par famille** (`is_armed_plan` : `EXECUTE_ORDER` coexistent, `WAKE` remplace le précédent du même symbole) — porter la logique `scheduler.py:149+` exactement (INSERT OR REPLACE par id, mais suppression du WAKE précédent du symbole).
- `active_indicator_watches` / `pop_expired_indicator_watches` : `SELECT WHERE expires_at <= now` ; `pop` **DELETE en transaction** et retourne les expirées ; `active` purge conditionnellement. Distinguer les deux (l'un trace, l'autre silencieux).
- `reconcile_universe` : `DELETE FROM {symbol_wake,stale_streaks,watches} WHERE symbol NOT IN (...)` **en une transaction**, idempotent (ne rien changer si univers identique — préserver le comportement « ne sauvegarde que si changement »).
- `_apply_decision_schedule` (daemon) enchaîne `remove_watch×N` + `set_watch` + `set_wake` : ces appels restent séparés (API inchangée) mais chacun devient atomique ; **noter** que l'atomicité de la SÉQUENCE complète (RC-5) viendra avec l'outbox du Lot B, hors périmètre Phase 1.
- Timestamps ISO `TEXT` (pas d'epoch) pour rester lisible par le shadow/cockpit.

### Tâches (TDD) — découpées par famille de méthodes
- [ ] **Task 13** — migration v3 + `import_scheduler_from_json` idempotent (promotion legacy `next_wake`, backup). Test : import fixture riche (symbols + streaks + watches avec order) → round-trip.
- [ ] **Task 14** — backend SQLite : réveils (`default`/`symbol` : set/clear/next_wake/due_symbols/symbols_with_wake/has_symbol_wake/seconds_until_wake). Test : parité JSON.
- [ ] **Task 15** — backend SQLite : stale streaks (get/set/reset). Test : parité + borne `STALE_BACKOFF_MAX_STREAK`.
- [ ] **Task 16** — backend SQLite : indicator watches (set avec coexistence par famille, active, pop_expired, remove). Test : coexistence EXECUTE_ORDER + remplacement WAKE ; expiration ; pop vs active.
- [ ] **Task 17** — `reconcile_universe` transactionnel + idempotent. Test : purge des symboles hors-univers sur les 3 tables ; univers identique → no-op.
- [ ] **Task 18** — double-write shadow `scheduler.json` (miroir `{default_next_wake, symbols, stale_streaks, indicator_watches}`) + câblage `daemon.py:3110`. Test : shadow miroir exact ; cycle daemon sim (réutiliser `test_daemon_exit_engine.py` en forçant le backend) sans régression.

**Point de validation Codex #4** : review Scheduler (coexistence des watches, reconcile transactionnel, parité des 17 méthodes, shadow) — le morceau le plus risqué.

---

## §1e — Migration des lecteurs hors-store + retrait du shadow

**Objectif** : basculer les lecteurs directs du JSON vers SQLite, puis retirer le double-write (SQLite devient l'unique source).

- [ ] **Task 19** — `read_models/runtime_state.py` : `_load_fills_safe` (`:294`), `_load_trade_plans_safe` (`:141`), lecture scheduler (`:184,462`) → requêtes SQLite (via `StateDb`, fail-safe conservé). Test : mêmes read-models qu'avant sur un même état.
- [ ] **Task 20** — `reporting/stats.py:136`, `rotation/collectors.py:69/71/97`, `runtime/cli.py:170-171`, `ui/tui.py:36`, `cockpit/app.py` → SQLite. Neutraliser les instanciations parasites (`create=False`). Test : cockpit/CLI/stats affichent le même état.
- [ ] **Task 21** — retrait du double-write shadow (les stores n'écrivent plus le JSON) + `CASYS_STATE_BACKEND=sqlite` devient le défaut. Garder un export JSON manuel (`scripts/export_state_json.py`) comme filet. Test : plus aucun `.json` d'état écrit en fonctionnement ; tout lu depuis SQLite.

**Point de validation Codex #5** : review finale — plus aucun lecteur JSON orphelin, retrait shadow sûr, défaut basculé.

---

## Stratégie de test (transversale)

- **Parité JSON↔SQLite** : le levier n°1. Pour chaque store, un test paramétré exécute la même séquence d'opérations sur les deux backends et compare l'état résultant (via l'API publique). Objectif : **prouver l'équivalence comportementale**.
- **Round-trip migration** : `JSON fixture → import → open/read → sérialiser → == fixture` (aux defaults patchés près).
- **Idempotence migration** : ré-import → no-op (tables non vides → skip).
- **Transactionnalité** : injecter une exception au milieu d'une opération multi-write (submit, sync_symbol_quantity, reconcile) → rollback, état inchangé.
- **Non-régression** : les tests existants (`test_execution.py`, `test_daemon_exit_engine.py`, `test_scheduler.py`, `test_tui.py`) restent verts **sans édition** en backend JSON, et re-passés en backend SQLite via le flag.
- **Shadow miroir** : après chaque write SQLite, le JSON shadow relu == projection des tables.

## Pièges — checklist explicite (à ne pas rater)

1. **Lecteurs hors-store** (§contexte) — 3 JSON lus directement à ~10 endroits. Le shadow les couvre jusqu'à §1e ; §1e les migre. Ne PAS basculer le défaut sqlite avant §1e.
2. **avg_price** recalculé en Python (long/short/reverse) — porter la logique, pas déléguer à SQL.
3. **Fills hétérogènes** — patcher les defaults à l'import.
4. **Positions `q==0` conservées** — stocker toutes les lignes, filtrer à la lecture.
5. **Instanciations parasites** (`collectors.py`) — ne pas créer d'état en lecture (`create=False`).
6. **Coexistence des indicator_watches par famille** — logique subtile, tester EXECUTE_ORDER vs WAKE.
7. **Atomicité de séquence multi-appels** (RC-5, `_apply_decision_schedule`) — **hors périmètre Phase 1** (chaque appel devient atomique ; la séquence complète = outbox Lot B). Le noter, ne pas prétendre le résoudre ici.
8. **Format ISO conservé** (pas epoch) pour le shadow/cockpit.
9. **Un seul `.db`** partagé (`state/casys.db`) — le `task_ledger` du Lot B s'y branchera pour l'outbox ; le `TaskLedger` de la Phase 0 (sa propre connexion) sera rebranché sur `StateDb` au Lot B (hors périmètre ici, à noter).
10. **Nom des dossiers de test** : `tests/state_db/` (sûr) — ne jamais nommer un dossier de test comme un module stdlib (`queue`, `state`) sous peine de shadow sys.path (cf. incident `tests/queue`→`tests/queue_ledger`).

## Ordre & jalons

`§1a` (substrat, Codex #1) → `§1b` (SimBroker POC, Codex #2) → `§1c` (TradePlanStore, Codex #3) → `§1d` (Scheduler, Codex #4) → `§1e` (lecteurs + retrait shadow, Codex #5). Chaque sous-phase est mergée indépendamment (flag `CASYS_STATE_BACKEND` réversible jusqu'à §1e).
