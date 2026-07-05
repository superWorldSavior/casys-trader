# Parité tool-round / multi-turn en mode `decide` via file (queue)

**Date** : 2026-07-05
**Statut** : 🟢 **DESIGN — décisions tranchées, 2 passes fact-check Codex intégrées (§10/10bis).**
Direction : services au boot (pas de barres dans le payload). Prêt à coder après GO Erwan.
**Issue** : #2 (prérequis technique de #1 « Brique 3 — analyse individuelle enrichie »).
**Branche/worktree** : `feat/queue-decide-tool-round` (`.claude/worktrees/queue-decide-tool-round`).
**Décisions (§11)** : Q1 = **B propre (resolver-service au boot)** · Q2 = **(a) mécanique
complète extensible, multi-tour paramétrable** · Q3 = **`None`** (issue #4) · Q4 = **tool
round moderne seul** (pas de `ContextResearchRequest` legacy).
**Fact-check** : V1 Codex intégré (§10) ; V2 sur cette révision à faire avant GO.

---

## 1. Contexte & problème

Depuis le Lot A (Phase 3, flag `CASYS_QUEUE_DECIDE_ENABLED`, **actif en paper**), les
décisions passent par une file durable : `decide_handler` → `decide_one` →
`codex_client.decide_batch(...)`. Or `decide_one` code **en dur** les deux flags à
`False` (`decide_one.py:84-85`) :

- `allow_context_request=False` → `REQUEST_CONTEXT` désactivé.
- `allow_tool_calls=False` → le tour d'outils n'est jamais pris (`client.py:179`) **et**
  le `_TOOL_CATALOG` n'est même pas injecté dans le prompt (`prompts.py:523`).

**Conséquence** : en mode queue l'agent décide **sans pouvoir appeler d'outils ni
faire de multi-turn**. Il perd notamment `get_active_plans` (anti-doublon),
`get_indicator_context`, `recall_learnings`. Le mode batch (`run_cycle` synchrone), lui,
a le tour d'outils complet (`planner_batch._run_tool_round`, `planner_batch.py:59-121`).
→ Le passage en queue est un **downgrade net** de la capacité de l'agent, silencieux.

Ce n'est **pas** une impossibilité architecturale : le `DecidePool` est un pool de
threads Python **in-process** (`decide_pool.py:96-99`) partageant la mémoire du daemon.
C'est une **dette de scope Lot A** (« YAGNI / itération future », `decide_one.py:13-19`).

## 2. Objectif / non-objectifs

**Objectif** : rendre le mode queue capable d'exécuter le tour d'outils **proprement et
de façon extensible** (décision Q2 = a) : services au boot (data_source, resolver, recall)
au lieu de closures par cycle, orchestration de round **paramétrable en nombre de tours**
(`max_rounds`, défaut aligné batch = 1 round + tour final). `recall_learnings` et
`get_indicator_context` **live et frais**, sous N workers concurrents.

**Non-objectifs (YAGNI)** :
- La **politique** multi-tour « approfondi sur signal fort + budget dédié » — reste #1
  Brique 3. Ici on construit la **mécanique** qui la rend triviale (paramètre `max_rounds`),
  on ne l'active pas au-delà de la parité batch. `ToolRoundLimits` = 24 calls / 3 par
  symbole (`core.py:191-196`) inchangé.
- Câbler `get_position_risk` / `get_recent_decisions` (= `None` même en batch, issue #4).
- Toucher le mode batch (inchangé).

## 3. Cartographie de l'existant (source de vérité)

### 3.1 Câblage au boot (`daemon.py:2402-2424`)
```
_task_ledger = _TaskLedger(STATE_DIR / "task_ledger.db")          # 2414
_decide_pools_obj = _ResourcePools({"acpx": _parallelism})        # 2416
_decide_pool = _DecidePool(
    ledger=_task_ledger, pools=_decide_pools_obj,
    handlers={"decide": _make_handler(codex_client=codex_client)},  # 2420 ⬅ SEUL arg
    num_workers=_parallelism, now_fn=time.time,
)
```
`make_decide_handler` ne reçoit **que** `codex_client` (`decide_handler.py:33-36`).
Pendant que le handler `execute_order` reçoit déjà `db/broker/plan_store/ledger`
(`daemon.py:2473-2478`) — précédent d'injection de deps dans un handler.

### 3.2 Ce dont le tour d'outils a besoin, et son scope
| Objet | Créé | Scope | Dispo au boot ? |
|---|---|---|---|
| `data_age_by_symbol` | `daemon.py:1088` | local `run_cycle` | non (mais **dans le payload**) |
| `market_context` (`execution_eligibility`) | `daemon.py:1096` | local `run_cycle` | non (mais **dans le payload**) |
| `active_watches_by_symbol` | `daemon.py:1397` | local `run_cycle` | non (mais **dans le payload**) |
| `attribution` | dans `base_context` | local `run_cycle` | non (**dans `shared_context`**) |
| `learnings_recall_provider` | construit `daemon.py:1052` via `_build_recall_provider` (défini `daemon.py:859`, store ouvert `:1040`) | local `run_cycle` | **oui au boot, MAIS `now` figé** → `now_fn` dynamique requis |
| `indicator_resolver` | closure `planner_batch.py:256-265` capturant **`tradable_bars_by_symbol`** | local `batch_decide` | **NON — dépend des barres du cycle** |

### 3.3 Ce que le payload contient déjà (`queue_dispatch.py:140-148`)
`per_symbol_facts` = `build_symbol_facts(...)` (`planner_batch.py:155-189`) →
`data_age_m` (int arrondi), `session`, `active_watches`, `execution`, `planning`,
`last_llm_review`. `shared_context` porte `attribution`.
⚠️ Format **transformé** (ex. `data_age_m` int, pas le float brut attendu par `ToolContext`).

### 3.4 Mécanique du tour d'outils (réutilisable telle quelle)
`ToolContext` (`core.py:76-94`), `execute_tool_round` (`core.py:266-273`),
`_run_tool_round` (`planner_batch.py:59-121`). Handlers : `get_active_plans`/`get_freshness`/
`get_attribution` lisent des **snapshots** ; `get_indicator_context`/`recall_learnings`
passent par **providers** ; `describe_data`/`find_indicators` sont **purs** (catalogue statique).

## 4. Architecture cible — services au boot (pattern factory/singleton)

Principe (validé avec Erwan) : **on ne trimballe pas les données dans le payload** ; on
distingue **ressources-singletons** (créées une fois au boot, partagées) et **services-factory**
(construits au boot au-dessus des singletons), exactement comme `make_execute_order_handler`
reçoit déjà `db`/`broker`/`plan_store` (`execute_order_handler.py:47-52`).

Le `ToolContext` du worker se compose de **deux sources** :

1. **Snapshots ← payload** (figés à l'enqueue, déjà présents) : `data_age`,
   `market_context`, `active_watches`, `attribution`. Reformatage facts→ToolContext
   (`data_age_m`→`data_age_by_symbol`, `execution`/`planning`→`market_context_by_symbol`).
   Ce sont des **faits du cycle** — figés est correct (ce sont ceux qui ont déclenché le decide).
2. **Services live ← boot** (injectés dans `make_decide_handler`) :
   - `data_source` : ⚠️ **PAS un singleton stable** (corrigé par fact-check V2, §10). En réalité
     le pool decide démarre **avant** que `data_source` soit construit (`daemon.py:2402-2424` vs
     `2486-2583`), et `data_source` est **remplacé** en cours de run (`daemon.py:2625,2775`) ou
     remis à `None` (`daemon.py:2785`). → on ne capture PAS l'objet ; on injecte une **indirection
     getter** `get_data_source: Callable[[], DataSource | None]` qui retourne la ref courante
     (si `None` → outil `unavailable`). Voir §5.
   - `indicator_resolver` : **service-factory** au boot au-dessus du getter + les bornes
     (`max_*`, `runtime_interval/lookback` — bootables, `daemon.py:149-150,946-947`) + l'**univers
     `symbols` du cycle** (⚠️ non bootable, doit venir du payload — voir §6). Remplace la closure
     `planner_batch.py:256`.
   - `learnings_recall_provider` : service-factory au boot (store `_recall_store` **SQLite locké**
     `store.py` + `now_fn` **dynamique**, pas un `now` figé).
   - `get_position_risk`/`get_recent_decisions` restent `None` (issue #4, orthogonal).

Le handler orchestre ensuite `run_tool_round` + tour final (factorisé de
`planner_batch.py:326-388`) **sur 1 symbole**, paramétrable `max_rounds` (défaut 1).

**Bénéfice vs « barres dans le payload »** : plus d'encodage `Bar`, plus de barres figées
(fetch frais), payload léger. **Coûts réels (fact-check V2)** : `data_source` ni stable ni
thread-safe (§5), throttle/thread-safety à traiter, univers dynamique à fournir.

## 5. Le resolver-service (Option B propre) — corrigé par fact-check V2

Décision Q1 = **B propre**. `indicator_resolver` devient un **service-factory au boot** au lieu
de la closure `planner_batch.py:256-265`. La factory s'appuie sur `resolve_indicator_requests` :

```
resolve_indicator_requests(raw_requests, bars_by_symbol, *, symbols, max_requests,
    max_indicators, default_window=48, market_get_bars=None, cached_interval="1h",
    cached_lookback="5d")                                          # context.py:285-296
```

Points confirmés (Codex V2) :
- Il ne prend **pas** `data_source` mais un callable `market_get_bars` (fallback `market.get_bars`,
  `context.py:302`). **Fetch-first** faisable avec `bars_by_symbol={}` + `market_get_bars=<getter>.get_bars` :
  il fetch le symbole si absent/timeframe différent (`context.py:322-334`) et les paires cross-asset
  manquantes (`context.py:337-358`).
- Il **requiert `symbols`** (univers) : filtre dur `symbol not in symbols` (`context.py:305-307`) et
  calcul des paires (`:337-343`). → l'univers du cycle doit être fourni (payload, §6).

### 5.1 ⚠️ Throttle : PAS une ressource de file (sinon deadlock — Codex W1)

Mon idée initiale « ajouter `yahoo` à `_ResourcePools` et l'acquérir dans le handler » **viole
l'invariant « une seule ressource par tâche »** (`2026-07-03-...-design.md` §4.1) : la tâche
`decide` tient déjà le permit `acpx` du claim au `finally` (`worker.py:65-73,111-113`) ; acquérir
un 2e permit **file** dedans = acquisition imbriquée = risque de **deadlock**.

**Solution retenue** : le throttle/thread-safety vit **dans `data_source`, pas dans la file**.
Un **wrapper thread-safe** (`threading.Lock` interne autour de `get_bars`) qui **sérialise** les
fetch. Ce lock est local à l'objet, de courte durée, **non imbriqué** avec les permits de la file
→ ne peut pas deadlock avec `acpx`. Il résout **d'un coup** W1 (throttle) **et** W3 (concurrence).

### 5.2 ⚠️ `data_source` : ni stable ni thread-safe (Codex W3)

- **Pas thread-safe** : `CompositeDataSource` mute `_last_source` / `_failed_sources_since_last_check`
  sans lock (`data_source.py:83-87,136,164,187`) ; `IBDataSource` mute `_ib` au reconnect
  (`ib_source.py:135`). → le wrapper à lock de §5.1 est **nécessaire**, pas optionnel.
- **Pas un singleton stable** : le pool decide démarre avant la construction de `data_source`, qui
  est ensuite remplacé/`None` (§4). → **indirection getter** obligatoire ; le wrapper à lock
  s'applique sur la ref **courante** retournée par le getter (si `None` → `unavailable`).

**Backpressure yahoo (limitation notée, V6)** : les `MarketError` sont avalées par
`resolve_indicator_requests` (`context.py:328-335,351-358`) et `execute_tool_call` transforme
toute exception en résultat compact (`core.py:223-235`) → aucune surcharge yahoo n'atteint
`Worker.on_overload`. **Voulu** en V1 (on ne veut pas que yahoo baisse le pool `acpx`), mais un
fetch yahoo lent n'est pas régulé → à surveiller (le coût peut monter, cf §9).

### 5.3 Throttle agnostique + timeout fetch (avis Codex, 2026-07-05)

Objectif métier (Erwan) : `get_indicator_context` doit devenir **fréquent et accessible** →
borner la concurrence des fetchs, **agnostique à la source** (yahoo remplaçable). Décision B
(thread-safety) faite ; le throttle est une **addition** validée en revue Codex :

- **Décorateur `ThrottledDataSource(inner, *, max_concurrent)`** implémentant le port
  `DataSource` (`ports.py`), avec un `BoundedSemaphore` local (≠ ressource de file → pas de
  deadlock W1, Codex A3). Agnostique : le mécanisme est générique, seule la limite dépend du
  fournisseur.
- **Par-source, pas global** (Codex A4) : le 429 est fournisseur-spécifique → envelopper les
  sources **concrètes** dans le dict `sources` (`yfinance = ThrottledDataSource(YFinance…, …)`),
  pas le composite en bloc.
- **Déléguer explicitement** `last_source` / `consume_failed_sources` / `disconnect` (Codex A1) —
  le daemon les appelle (`daemon.py:2759,264`) ; sinon reporting/détachement IB/fermeture cassés.
- **Sémaphore bloquant AVEC timeout** (Codex A2) : `acquire(timeout=…)` → `MarketError("data_source_throttled")`,
  pas de blocage indéfini ni de `blocking=False` pur (dégrade trop vite en unavailable).
- **⚠️ Timeout de fetch PRIORITAIRE** (Codex A6) : `market.get_bars` appelle
  `yf.Ticker(...).history(...)` **sans timeout** (`market_data.py:644`). Avec throttle, un hang
  garde un permit *ad vitam*. → **traiter le timeout fetch AVANT/AVEC le throttle**, sinon
  augmenter l'usage de `get_indicator_context` amplifie le risque de gel.
- **Token bucket** (débit/minute) : plus juste pour l'anti-429 strict, mais **différé** —
  `sémaphore + timeout + métriques` d'abord ; token bucket seulement si les 429 persistent (A5).

## 6. Changements par fichier

1. **`build_indicator_resolver` (nouveau, factory) — ✅ FAIT (T1a).** Contrat **étroit
   fetch-first** (AX #7/#9) : `build_indicator_resolver(*, symbols, get_bars, max_requests,
   max_indicators, default_window=48)` → resolver `(requests)->ToolPayload`. `get_bars` **requis**
   (pas de défaut = pas de footgun), `bars_by_symbol` **non exposé** (toujours `{}` en interne :
   un seul comportement, pas de « second régime » implicite). Le **batch garde sa closure**
   (il a un vrai cache) — pas de réutilisation forcée (YAGNI). `context.py`, 2 tests verts.
2. **`data_source` thread-safe + getter (§5.1/5.2)** — wrapper `threading.Lock` autour de
   `get_bars` (`data_source.py`), + un **getter** `Callable[[], DataSource | None]` (le pool
   démarre avant la construction → ne pas capturer l'objet ; lire la ref courante, `None` →
   `unavailable`). ⚠️ à défaut, **réordonner le boot** pour construire `data_source` avant le pool.
3. **`decide_handler.py` / `make_decide_handler`** — accepter `indicator_resolver` +
   `learnings_recall_provider`, comme `make_execute_order_handler` reçoit ses stores
   (`execute_order_handler.py:47`).
4. **`daemon.py` (~2402-2424)** — construire les services au boot (getter data_source,
   `build_indicator_resolver`, `build_recall_provider` avec `now_fn` **dynamique** — ⚠️
   `learnings_recall.py:37,53,78` fige `now`) et les passer à `_make_handler`.
5. **Payload `queue_dispatch.py`** — snapshots déjà présents (`per_symbol_facts` +
   `shared_context.attribution`) ; `now` = `now_fn()` dans le worker. **Ajouter l'univers
   `symbols`** (`analysis_symbols` du cycle, `daemon.py:1438`) — requis par le resolver (§5).
6. **`decide_one.py`** — params (resolver, recall provider, univers) ; `allow_tool_calls=agent_tools_enabled`,
   `allow_context_request=False`, **`use_symbol_calls_contract=True`** (condition W4). ⚠️ le guard
   qui rejette un `BatchToolCallRequest` est le **non-dict** (`decide_one.py:97`) → le remplacer
   par l'orchestration du round (`max_rounds`).
7. **`planner_batch` — factoriser TOUTE l'orchestration en fonction grain-1 réutilisable.**
   Pas seulement `_run_tool_round` (results+runtime, `:327`) : aussi filtrage symbole (`:340`),
   injection `tool_results` (`:346`), 2e `decide_batch` flags=false (`:350`), blocage tool-loop
   (`:373`), et le **merge `domain_tools`** (`:381`) — **obligatoire** (sinon traces + note_ids
   de recall perdus, V5). Boucle `max_rounds` (défaut 1) au lieu des chunks.
8. **Q4 — pas de `ContextResearchRequest` legacy** : `allow_context_request=False` suffit (les
   décisions ne deviennent plus `ContextResearchRequest`, `parsing.py:563-568`) ; `get_indicator_context`
   = voie moderne (`prompts.py:491`). Combo confirmé supporté (Codex W4) **si** `use_symbol_calls_contract=True`.
9. **`queue_dispatch.py` (budget, ~258)** — `model_calls_used = len(decisions_by_symbol)` ne
   compte pas le 2e appel. Le handler remonte les appels réels (rounds + final) pour un fusible juste.

## 7. Séquencement (TDD — chaque étape réversible)

- **T1** — `build_indicator_resolver` (factory, pt 6.1) + extraction de l'orchestration
  grain-1 réutilisable (round + tour final + merge `domain_tools`), paramètre `max_rounds`
  (test : mêmes entrées → même round qu'en batch ; `domain_tools` mergé).
- **T2** — Wrapper `data_source` thread-safe (`threading.Lock` autour de `get_bars`) + getter
  de ref courante (`None` → `unavailable`) (test : N `get_bars` concurrents sérialisés, aucune
  course sur `_last_source`/`_failed_sources` ; ref remplacée en cours → getter suit). **PAS** de
  ressource `yahoo` dans la file (W1 : deadlock).
- **T3** — Injecter `indicator_resolver` + `learnings_recall_provider` (`now_fn` dynamique)
  au boot → `make_decide_handler` (test : handler construit avec services non-`None` ; `now`
  non figé).
- **T4** — `decide_one` : `allow_tool_calls=True` / `allow_context_request=False`,
  reformatage facts→`ToolContext`, orchestration `max_rounds`, gestion `BatchToolCallRequest`
  (test : un round produit une `Decision` finale ; pas de `RetryableError` sur le round).
- **T5** — Contrat `model_calls_used` = appels réels (rounds + final) + fusible (test).
- **T6** — Thread-safety N workers : `data_source` concurrent (+ permit yahoo), `_recall_store`
  SQLite locké (`store.py`) (test : pas de course, pas de 429 non borné).
- **T7** — Équivalence : même symbole, mêmes données → décision queue ≈ décision batch,
  traces + note_ids de recall persistés (`recalls`).

## 8. Budget & fusibles

- Tool round = **jusqu'à 2 appels acpx** par décision (round + final) avec `max_rounds=1`
  (défaut) ; `CASYS_QUEUE_TOOL_MAX_ROUNDS` borne les allers-retours.
- Fusible d'admission **halvé uniquement si le round est réellement câblé**
  (`tools_active` = agent_tools ET services boot — review T4 E4a) ; `max_model_calls<1`
  = fusible fermé (0 admis). `model_calls_used` = somme des appels réels remontés en
  enveloppe (clampés ≥1).
- Bornes d'outils **grain-1 : 24 calls/round, 8/symbole** (vs 24/3 calibré batch chunk) —
  les outils s'exécutent localement, ce relèvement ne coûte aucun appel acpx.
- Limitation notée (E4b) : les tâches dead/budget-expired ne remontent pas leur coût
  réel (appels déjà consommés invisibles du fusible) — à mesurer en observation.

## 8bis. État d'implémentation (2026-07-05)

**LIVRÉ sur `feat/queue-decide-tool-round`** — T1 (primitives, mergé main) ; T2
(data_source thread-safe I/O-hors-verrou + indirection + `ThrottledDataSource`) ; T3
(`DataSourceHandle` + synchro unique, throttle branché `CASYS_YFINANCE_FETCH_CONCURRENCY`,
recall `now_fn`) ; T4 (câblage bout-en-bout : services boot → handler → `decide_one`
outillé, enveloppe `model_calls`, univers payload, fusible corrigé). 4 reviews Codex
exhaustives intégrées (T1/T2/T3/T4). Timeout fetch curl_cffi = issue #9 ; sessions
éphémères multi-tour profond = issue #10 ; risk/recent_decisions = issue #4.
⚠️ Déploiement : redémarrer le daemon après merge (code_version) sinon l'ancien
mode dégradé continue de tourner.

## 9. Risques & points ouverts

- **`data_source` ni stable ni thread-safe** (Codex W3, confirmé) : mutations sans lock
  (`data_source.py:83-87,136,164,187`, `ib_source.py:135`) + objet remplacé/`None` en cours de
  run (`daemon.py:2625,2775,2785`) + pool démarré avant sa construction. → wrapper lock + getter
  (§5.1/5.2). **C'est le vrai coût de la direction propre**, pas trivial.
- **Throttle = wrapper lock interne, PAS une ressource de file** (Codex W1) : une ressource
  `yahoo` acquise dans le handler violerait « une seule ressource par tâche » (deadlock). Le lock
  local sérialise les fetch sans imbrication avec `acpx`.
- **Coût yahoo sous-estimé** (Codex V6) : un round peut exécuter jusqu'à **24 tool calls**
  (`core.py:191-196`), et un `get_indicator_context` fetch symbole **+ paires** (`context.py:337-358`).
  Fetch-first sans cache = pas « rare ». À mesurer ; cache de barres (§5, différé) à rouvrir si le coût monte.
- **Combo `allow_tool_calls=True` + `allow_context_request=False`** : le batch lie les deux
  (`planner_batch.py:309`) ; ce combo n'est peut-être pas exercé → **vérifier prompt + parsing**
  (`client.py:179`, `parse_batch_or_tool_calls`). Point clé du re-fact-check.
- **Purge de tâches `running` en vol** (Codex V6) : `delete_stale_decide` supprime aussi des
  `running` d'un ancien cycle (`ledger.py:300,312`) sans annuler le thread LLM → à vérifier vs
  un round multi-appels (2+ appels par décision, fenêtre plus longue).
- **Format facts→ToolContext** : `build_symbol_facts` transforme (`data_age_m` int, pas le
  float brut) → mapping explicite requis, testé.
- **Cohérence télémétrie** (Codex V5) : `asdict(decision)` préserve `domain_tools`
  (`types.py:28`, reconstruit `queue_dispatch.py:215`) et la persistance suit
  (`decision_entries.py:29,90` → `decision_recorder.py:91,130,149` → `store.record_recall`).
  **Condition** : réutiliser le `replace(... domain_tools=...)` (`planner_batch.py:381`) ;
  l'omettre perd traces + note_ids de recall.

## 10. Fact-check Codex (2026-07-05, intégré)

Revue adversariale Codex (session `spec-queue-tool-round`, lecture seule). Findings
intégrés ci-dessus. Résumé :

- **V1 (câblage) — NUANCE.** §3 globalement juste. Correctifs : `_build_recall_provider`
  **défini** `daemon.py:859` (construit dans `run_cycle` `:1052`, store ouvert `:1040`) ;
  payload `queue_dispatch.py:140-148` (pas 139-147) ; attribution `base_context` `:1207`.
  « recall reconstructible au boot » **sur-vendu** : le provider fige un `now` → `now_fn`
  dynamique requis.
- **V2 (option A) — FAUX / sous-estimé.** Source = `analysis_bars_by_symbol` (fallback daily),
  pas `tradable_bars`. Barres = `Bar` dataclasses (encoder). Le payload ne porte ni barres ni
  bornes. Surtout : `resolve_indicator_requests` **fait de l'I/O live** (paires `context.py:345`,
  timeframes hors cache `:322`) → « pas d'I/O worker » faux. → §5 refondu (A′/B/D).
- **V3 (`decide_one`/tour final) — NUANCE, sous-vendu.** `BatchToolCallRequest` rejeté par le
  guard **non-dict** (`decide_one.py:97`). `_run_tool_round` **ne suffit pas** : factoriser
  toute l'orchestration finale (`:340-388`), dont le merge `domain_tools`. Parité complète
  inclut **aussi** `ContextResearchRequest` (`:412,437`).
- **V4 (fraîcheur) — NUANCE, sous-estimé.** Sous contention, barres figées de plusieurs minutes
  (budget 900 s). Purge de `running` stale sans annuler l'appel en vol.
- **V5 (télémétrie) — VRAI sous condition.** Traces/recalls persistés **si** merge `domain_tools`
  réutilisé.
- **V6 (obstacles ratés).** now recall figé ; payload mono-symbole casse le cross-asset ;
  `ContextResearchRequest` ; `model_calls_used = len(decisions)` à re-contracter ; purge en vol.

### 10bis. Fact-check V2 (révision resolver-service, intégré)

- **W1 — VRAI / STOP.** Ressource `yahoo` dans la file = acquisition imbriquée avec `acpx` →
  viole « une seule ressource par tâche » (deadlock). → throttle = **lock interne data_source**,
  pas une ressource de file (§5.1).
- **W2 — NUANCE (B survit).** `resolve_indicator_requests` prend `market_get_bars` (pas
  `data_source`) + `symbols` **requis** (`context.py:285-307`). Fetch-first OK avec `bars={}`,
  mais l'univers du cycle doit être fourni (payload).
- **W3 — FAUX / risque réel.** `data_source` ni thread-safe (`data_source.py:83-87`) ni singleton
  stable (remplacé/`None`, pool démarré avant). → wrapper lock + getter (§5.2).
- **W4 — VRAI si `use_symbol_calls_contract=True`.** Combo `tool_calls`/`context_request=False`
  supporté (`client.py:179`, `parsing.py:599-604`).
- **W5 — NUANCE.** Bornes bootables ; univers `analysis_symbols` non bootable → payload.
- **W6.** Coût yahoo (24 calls/round possible) ; pas de backpressure yahoo (erreurs avalées).

## 11. Décisions (tranchées par Erwan, 2026-07-05)

- **Q1 → Option B propre.** `indicator_resolver` = service-factory au boot au-dessus du
  `data_source` singleton ; fetch frais ; pas de barres dans le payload. + ressource `yahoo`.
- **Q2 → (a) mécanique complète et extensible.** Orchestration `max_rounds` paramétrable
  (défaut 1 = parité batch) ; la **politique** multi-tour approfondi reste #1 Brique 3.
- **Q3 → `None`.** `get_position_risk`/`get_recent_decisions` non câblés (issue #4).
- **Q4 → tool round moderne seul.** `allow_tool_calls=True`, `allow_context_request=False` ;
  pas de `ContextResearchRequest` legacy (`get_indicator_context` = voie moderne, `prompts.py:491`).

> Rationale commun : « on n'est pas pressés, on fait les choses bien » (Erwan). Pas de MVP
> dégradé ; services propres réutilisables, chaque étape testée + reviewée Codex.
