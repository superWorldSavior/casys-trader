# Architecture — casys-trader

> Ce document décrit l'architecture technique du daemon de trading paper piloté par LLM.
> **Généré par analyse statique du code — à re-vérifier si l'architecture évolue.**
>
> Refactor modulaire en place sur `main` par tranches compatibles.
> Les tranches récentes sont suivies dans `docs/superpowers/plans/`
> (`tools-market-data-boundary`, `tools-news-feed-boundary`,
> `tools-portfolio-boundary`, `tools-memory-boundary`).

---

## 1. Vue d'ensemble

casys-trader est un **daemon de trading paper** qui tourne en boucle de cycles.
Le LLM n'est **pas** un opérateur continu : il est un **planificateur** qui conçoit des
scénarios d'entrée (plans armés, veilles) que le daemon exécute mécaniquement, et
qui se réveille sur événement pour les ajuster.

Deux boucles se superposent :

| Boucle | Acteur | Cadence | But |
|--------|--------|---------|-----|
| **Boucle 1** | Humain | ad hoc | Mandat, guardrails, universe config |
| **Boucle 2** | Daemon + LLM | cycle (x–x+1 min) | Décision, exécution, logging |

Le code est le seul à calculer les indicateurs, évaluer les plans, appliquer les
gates. Le LLM reçoit les faits calculés, raisonne, et retourne des artefacts
structurés (décisions JSON, plans, veilles). `trader/runtime/daemon.py`

### 1.1 Carte modulaire actuelle

`trader/runtime/daemon.py` reste l'entrypoint runtime et l'orchestrateur du cycle. Les
responsabilités répétables ou testables ont été sorties vers des modules
cohésifs, avec wrappers de compatibilité quand des tests ou imports historiques
les utilisaient :

| Zone | Rôle | Notes |
|---|---|---|
| `trader/application/planner_batch.py` | Batch LLM, budget modèle, tournée d'outils, REQUEST_CONTEXT | appelé via `daemon._batch_decide()` |
| `trader/application/market_snapshot.py` | Barres runtime/daily/exit, fraîcheur, FX, eligibility, tradable maps | retourne `MarketSnapshot`, le daemon l'unpack |
| `trader/application/decision_recorder.py` | Enrichissement décision, ledger, report, status, event, recall traces | source durable : `state/decisions.jsonl` |
| `trader/application/cycle_schedule.py` | Politique applicative de réveil : bornes explicites, backoff stale, due symbols, veilles et événements de réveil | le daemon garde des wrappers privés de compatibilité |
| `trader/application/confidence_feedback.py` | Feedback persistant des rejets de gate confiance vers les learnings de l'agent | le daemon conserve le wrapper privé historique |
| `trader/application/execution_eligibility.py` | Classification execution/planning par symbole et raison de blocage d'exécution | utilisé par `market_snapshot` et wrappers privés du daemon |
| `trader/application/exit_bars.py` | Fetch/validation des barres fines de sortie et calcul high/low de fenêtre pour plans ouverts | branché comme `exit_bars_fetcher` dans `market_snapshot`, wrappers privés du daemon |
| `trader/application/order_admission.py` | Helpers purs d'admission : intent, résolution position-aware, clamp sortie, stop, risk metrics | l'orchestration RiskGate/broker reste dans `trader/runtime/daemon.py` |
| `trader/application/risk_capacity.py` | Contexte de capacité exposé à l'agent : gross exposure, plafonds buy/sell, quantités natives FX-aware | le daemon injecte le broker, les prix, les FX et la fonction devise |
| `trader/application/watch_scanner.py` | Scan applicatif des indicator/exit watches : fetch des barres, évaluation, cooldown, retrait et réveil symbole | le daemon conserve l'émission d'événements/logs runtime |
| `trader/agent/` | Contexte agent, mémoire mandat/stratégie, façade planner, transport LLM/acpx | compat : `trader.agent_context`, `trader.codex_client`, `trader.llm`, `trader.tools.memory.Memory` |
| `trader/agent_protocol/` | Types, prompts, parsing du contrat LLM | utilisé par `trader/agent/client.py` |
| `trader/agent_tools/` | Package des outils domaine lecture seule | `registry.TOOL_REGISTRY` assemble 9 handlers |
| `trader/semantic/` | Catalogue sémantique des indicateurs et requêtes agent | évite de disperser les IDs/request contracts |
| `trader/domain/` | Primitives neutres (`Bar`, `MarketError`, `Side`) | évite que `market`/`planning` importent `tools` |
| `trader/scheduling/` | Réveils globaux/par symbole, stale backoff, veilles persistées | compat : `trader.tools.scheduler` |
| `trader/planning/` | Plans de trade, veilles, exit engine, gate de pertinence | compat : `trader.trade_plan`, `trader.indicator_watch`, `trader.exit_engine`, `trader.relevance_gate` |
| `trader/execution/` | Contrats `Order`/`Fill`, ports `Broker`/`CommissionModel`, broker paper, commissions, RiskGate, projection portefeuille | contrats/ports : `trader.execution.contracts`, `trader.execution.ports`; compat : `trader.tools.execution`, `trader.tools.portfolio`, `trader.risk` |
| `trader/learnings/` | Buffer brut JSONL, store SQLite recall, embeddings, consolidateur | compat : `trader.tools.memory.LearningsStore`, `trader.learnings_store`, `trader.embeddings`, `trader.consolidator` |
| `trader/market/` | Port `DataSource`, adaptateurs yfinance/IB/composite, fraîcheur, indicateurs, FX, news, macro, radar, régime, priorisation gross exposure | port : `trader.market.ports.DataSource`; compat : `trader.tools.market`, `trader.tools.data_source`, `trader.tools.ib_source`, `trader.tools.news_feed`, `trader.fx`, `trader.features`, etc. |
| `trader/queue/` | File de tâches durable, workers, pools, backpressure | backend technique utilisé par le runtime queue-on |
| `trader/state_db/` | Backend SQLite de l'état paper, broker store, outbox | source durable quand `CASYS_STATE_BACKEND=sqlite` |
| `trader/config/` | Loaders de configuration runtime (`pool`, `portfolio`) | retire les loaders transverses de la racine `trader/` |
| `trader/rotation/` | Rotation d'univers, hot-sets par venue, schedule, override, ledger rotation | `trader.rotation` réexporte l'ancien core |
| `trader/metadata/` | Métadonnées git/code version | utilisé par runtime et reporting sans cycle |
| `trader/reporting/` | Ledger décision, raisons, audit ex-post, attribution, stats, tool usage, meta-performance | alias compat via `trader.__init__` et shims legacy plats |
| `trader/commands/` | Entry points CLI canoniques (`stats`, `attribution`, `tool_usage`, `tui`) | compat : `python -m trader.stats`, `python -m trader.attribution`, etc. |
| `trader/system/` | Helpers système neutres (`process_env`) | partagé par agent/cockpit/runtime sans dépendance runtime |
| `trader/runtime/` | Daemon, CLI, logging, PID file, IB attach, rotation ledger, writers d'état fichier | `trader.daemon` et `trader.cli` sont des packages proxy pour `python -m` |
| `trader/read_models/runtime_state.py` | Lecture tolérante des fichiers `state/` pour TUI/cockpit | ne participe pas aux décisions live |
| `trader/cockpit/` | App Textual, événements cockpit, supervisor local | `trader.cockpit` reste runnable |
| `trader/ui/` | Builders Rich purs, TUI textuelle, palette | `trader.tui` reste une façade import/CLI legacy |
| `trader/attribution/`, `trader/tui/`, `trader/daemon/`, `trader/cli/`, `trader/tools/`, `trader/stats.py`, `trader/tool_usage.py` | Façades de compatibilité import/CLI | doivent rester fines et déléguer vers les packages canoniques |

### 1.2 Niveaux d'architecture

Le répertoire `trader/` reste volontairement peu profond pendant la migration,
mais ses packages ne sont pas tous du même niveau :

| Niveau | Packages | Règle pratique |
|---|---|---|
| Composition runtime | `runtime/`, `commands/`, wrappers `daemon`/`cli` | peut assembler les dépendances et déclencher les side effects |
| Services applicatifs | `application/` | orchestre un cas d'usage testable sans être l'entrypoint process |
| Capacités métier | `market/`, `planning/`, `execution/`, `scheduling/`, `learnings/`, `rotation/`, `agent/`, `agent_protocol/`, `agent_tools/` | porte la logique du domaine et ne dépend pas de `runtime/` |
| Primitives transverses | `domain/`, `metadata/`, `system/`, `config/` | types/helpers stables, sans dépendance montante |
| Read models et surfaces | `reporting/`, `read_models/`, `ui/`, `cockpit/` | lit l'état produit par le runtime, ne décide pas à sa place |
| Compatibilité legacy | `tools/`, `attribution/`, `tui/`, `daemon/`, `cli/`, modules `stats.py`/`tool_usage.py` | délègue vers le canonique ; aucun nouvel import interne ne doit viser ici |

La cible n'est donc pas forcément de créer six dossiers parents (`core/`,
`infra/`, etc.) d'un coup. Le travail en cours est d'abord de rendre le niveau de
chaque module explicite, puis de réduire `tools/` à une couche de compatibilité.

Le choix volontaire reste de ne pas frameworkiser en `ports/`/`adapters`
génériques. En revanche, trois packages neutres existent maintenant parce qu'ils
suppriment des cycles réels :

- `domain/` porte les primitives stables partagées par `market`, `planning` et
  les façades legacy ;
- `metadata/` porte la version git utilisée par `runtime` et `reporting` ;
- `system/` porte les helpers de processus utilisés par l'agent et le cockpit.

Les anciens imports restent compatibles quand ils existaient déjà
(`trader.tools.market.Bar`, `trader.tools.execution.Order`, `trader.tools.scheduler.Scheduler`,
`trader.runtime.code_version`, `trader.process_env`, `trader.stats`,
`trader.attribution`, `trader.tool_usage`, `trader.tui`), mais les imports internes
doivent viser les packages neutres ou canoniques (`domain/`, `execution/broker`,
`execution/contracts`, `execution/ports`, `market/`, `market/ports`, `metadata/`,
`system/`, `commands/`). Les adaptateurs concrets restent dans
`execution/broker` et `market/data_source` quand la composition runtime les
instancie. Les tests `tests/test_package_layout.py`, `tests/test_code_version_imports.py`
et `tests/test_runtime_pid_file.py` gardent ces frontières.

Les nouveaux lots ne doivent pas ajouter de dépendances montantes hors
composition root. Les dépendances montantes acceptées sont concentrées dans
`trader/runtime/daemon.py`, qui orchestre les side effects ; les modules métier
ne doivent pas importer le runtime pour accéder à des primitives ou à des
métadonnées.

---

## 2. Diagramme ASCII — flux d'un cycle

```
Scheduler (timer / indicator_watch trigger)
    │
    ▼
run_cycle()                                     [trader/runtime/daemon.py]
    │
    ├─ Chargement config (universe.yaml, risk.yaml)
    ├─ Reload univers (rotation_daemon: compose_active_universe) ─── D9/D10
    │
    ├─ market_snapshot.build_market_snapshot()
    │    ├─ Fetch barres marché (15m × 5j)
    │    ├─ assess_freshness → stale? → backoff exponentiel
    │    ├─ Fetch barres daily (1y) → cockpit daily
    │    └─ Fetch barres 5m (plans ouverts) → exit checks fins
    │
    ├─ _apply_planned_exits()  ──────────────────  chemin sortie AUTO
    │    └─ exit_engine.evaluate_plan(hard_stop|TP|trailing|max_hold|profit_protection)
    │
    ├─ _scan_exit_watches()                       réveil depuis exit_watch
    ├─ _scan_indicator_watches()                  réveil depuis sched watches
    │
    ├─ build_market_cockpit() → shared_context   [trader/agent/context.py]
    │    (cockpit compact, KPIs, attribution, regime_families, learnings)
    │
    ├─ Gate de pertinence (relevance_gate)  ──── D7 étage A
    │    └─ quiet? → HOLD sans LLM (quiet_gate)
    │
    ├─ Plans armés (EXECUTE_ORDER) ─────────────  D7 étage B
    │    └─ resolve_exit_plan() sur vol fraîche → exécution SANS LLM
    │
    ├─ planner_batch.batch_decide() ─ 1 appel LLM pour tous les symboles dus
    │    └─ codex_client.decide_batch()
    │         ├─ AcpxBackend (acpx --format quiet exec)
    │         └─ round-trip optionnel REQUEST_CONTEXT (indicateurs à la demande)
    │
    └─ Pour chaque décision :
         ├─ order_admission helpers / exit_plan
         ├─ resolve_exit_plan() (direct OPEN_LONG/SHORT) ─── unification D11
         ├─ RiskGate.check_confidence() + RiskGate.check()
         ├─ SimBroker.submit() → fill
         ├─ create_trade_plan() → TradePlanStore
         ├─ DecisionRecorder.record() → decisions.jsonl + current_report/status
         └─ _apply_decision_schedule() → Scheduler (next_wake, indicator_watch)
```

---

## 3. Le cycle de décision — de bout en bout

### 3.1 Sélection des symboles dus

`trader/runtime/daemon.py::_select_due_symbols` — en mode normal :
`Scheduler.due_symbols()`
sélectionne les symboles dont le `next_wake` est passé. En mode `--once`/`--bootstrap` :
tout l'univers.

L'univers actif est généré par la rotation (D9/D10) à chaque cycle :
`trader/rotation/daemon.py` appelle `compose_active_universe(now)` qui compose
`sticky_all ∪ union(hot-lists des marchés ouverts)` et écrit
`config/universe.yaml` si le contenu change (`trader/runtime/daemon.py`).

### 3.2 Chargement des barres & fraîcheur

`trader/application/market_snapshot.py` construit le snapshot du cycle :
barres 15m / 5j (runtime décisionnel) + barres 1j / 1y (cockpit daily).
`market.assess_freshness()` est le garde « marché live » : une dernière barre trop
vieille (> 40 min par défaut) → `stale_market_data` → le symbole
est exclu du tradable et reçoit un backoff exponentiel (`trader/runtime/daemon.py`).

Barres 5m (fenêtre 1j) fetched **uniquement** pour les symboles avec un plan ouvert,
pour la détection fine intra-barre des stops/TP.

### 3.3 Construction du contexte partagé

`build_market_cockpit()` (`trader/agent/context.py`) produit le tableau compact (cols `s`,
`r`, `vol`, `z`, `er`, `ac`, `rs`, `sz`, régime daily, frais `be_ref_bps`/`rtrip_bps`).

Le `base_context` injecté au LLM contient (`trader/runtime/daemon.py`) :
- `cockpit` — tableau cross-asset
- `portfolio` — snapshot (holdings, equity, cash, frais estimés)
- `risk_limits` — config `risk.yaml`
- `kpis` — stats live (win%, P&L, etc.)
- `attribution` — P&L par raison de sortie, calibration confidence
- `regime_families` — biais directionnel calculé par famille thématique (D2)
- `learnings` — guardrails + patterns consolidés (D6)
- `stale_market_data` — liste des symboles exclus ce cycle
- `semantic.requestable_indicator_ids` — indicateurs disponibles via REQUEST_CONTEXT

Faits calculés par le code et injectés (principe AX : pas de prose) :
- `data_age_m` (âge réel en min des barres) — `trader/runtime/daemon.py`
- `session` (état de la séance par place) — `market.session_snapshot()`
- `active_watches` (résumé des veilles actives par symbole)

### 3.4 Gate de pertinence (D7 étage A)

`relevance_gate.symbol_needs_llm()` (`trader/planning/relevance_gate.py`) — fonctions pures.
Passe au LLM uniquement si : réveil agent (`agent_wake`), trigger, position ouverte,
régime fort (≥ 70 % de la famille), signal cockpit (`sig`/`stretched`),
ou revue périodique garantie (4 h). Sinon → `quiet_gate` (HOLD sans appel).
`trader/runtime/daemon.py`

### 3.5 Plans armés — exécution sans LLM (D7 étage B)

Les `indicator_watch` à `on_trigger: EXECUTE_ORDER` portent un `order` complet
(intent, qty, confidence, exit_plan). Au déclenchement (`trader/runtime/daemon.py`) :
1. `resolve_exit_plan()` — résolution late-binding du stop/TP sur vol fraîche (D11)
2. `armed_order_price_coherent()` — vérif que le prix n'a pas déjà franchi le stop
3. Si conflit multi-scénarios même symbole → réveil planificateur, pas d'exécution
4. Si stale ou position déjà ouverte → annulation + réveil planificateur

Le scénario validé crée une `Decision` directement, sans appel LLM.

### 3.6 Appel LLM batch — `planner_batch.batch_decide()`

Un seul appel `codex_client.decide_batch()` pour tous les symboles dus & frais.
`daemon._batch_decide()` est un wrapper de compatibilité vers
`trader/application/planner_batch.py`. Le contexte partagé est envoyé une fois
(économie D7).

Round-trip `REQUEST_CONTEXT` optionnel : si le LLM demande des indicateurs
supplémentaires (`ContextResearchRequest`), `resolve_indicator_requests()` les calcule
à partir des barres déjà en mémoire et lance un 2e batch sans ré-appeler Codex une
3e fois. Budget : `max_model_calls_per_cycle` (défaut 25). `trader/runtime/daemon.py`

Transport : `AcpxBackend.complete()` (`trader/agent/llm.py`) → `acpx --format quiet --allowed-tools "" --no-terminal exec [prompt]`.
Sessions jetables (isolation/idempotence). Fallback : `OpenAICompatibleBackend` (Ollama) si `TRADER_OLLAMA_API_KEY` défini.

### 3.7 Validation & gates pré-exécution

Pour chaque décision (`trader/runtime/daemon.py`) :

| Vérification | Code rejet |
|---|---|
| Intent valide vs action | `invalid_intent` |
| Exit_plan parsable | `invalid_exit_plan:*` |
| `hard_stop` du bon côté | `invalid_exit_plan:hard_stop_wrong_side` |
| Ouverture sans stop → bloqué | `risk:missing_hard_stop` |
| Confiance ≥ seuil adaptatif | `risk:confidence_below_required` |
| RiskGate.check() | `risk:order_value_exceeded`, `risk:gross_exposure_exceeded`, etc. |

`resolve_exit_plan()` est appliqué aux entrées directes `OPEN_LONG`/`OPEN_SHORT`
(unification avec les armés, D11).

Les helpers purs de cette admission vivent dans
`trader/application/order_admission.py` : intent, hard-stop, clamp de sortie,
quantité ouverte d'un reverse, et métriques de risque d'entrée. L'orchestration
complète reste volontairement dans `trader/runtime/daemon.py` pour préserver l'ordre exact des
side effects : scheduling, recorder, `RiskGate`, broker, trade plans et
performance model.

### 3.8 Exécution et persistance

`SimBroker.submit()` → `Fill`. Post-fill :
- `_append_model_performance()` → `RuntimeStateWriter.append_model_performance()` → `state/model_performance.jsonl`
- `create_trade_plan()` → `TradePlanStore` (`state/trade_plans.json`)
- `DecisionRecorder.record()` → `decision_ledger_store.append()` →
  `state/decisions.jsonl`
- `_apply_decision_schedule()` → Scheduler (next_wake, indicator_watch créée/annulée)
- `_write_current_report()` → `RuntimeStateWriter.write_current_report()` → `state/current_report.json`

---

## 4. Les chemins de sortie

Trois chemins distincts. Voir `docs/analysis/comportement-sorties.md` pour le détail
chiffré et les analyses P&L par raison.

### 4.1 Sortie automatique — exit_engine

`exit_engine.evaluate_plan()` (`trader/planning/exit_engine.py`) — priorité stricte :
```
hard_stop > max_hold > take_profit > profit_protection > trailing_stop
```

- **hard_stop** : vérifié sur `min(price, bar_low)` (LONG) ou `max(price, bar_high)` (SHORT).
  Fill conservateur : jamais meilleur que le niveau du stop.
- **max_hold** : expiration temporelle (`plan.max_hold_minutes`).
- **take_profit** : paliers fractionnés. `after_fill` peut valoir `close` ou
  `move_stop_to_breakeven`.
- **profit_protection** : réduction partielle (`close_fraction`) quand le drawback
  depuis le high watermark dépasse `trigger_on_giveback_pct`, armé à `arm_at_r` × R.
- **trailing_stop** : types `price` / `percent` / `volatility_multiple`.

Barres 5m agrégées sur `EXIT_CHECK_WINDOW_BARS` (3 × 5 min) pour les plans ouverts
— évite les spikes manqués entre deux barres 15m. Garde temporelle : seules les
barres `ts >= plan.opened_at` comptent. `trader/runtime/daemon.py`

### 4.2 Sortie discrétionnaire LLM

`intent: CLOSE | REDUCE | REVERSE` → tag `exit_reason = "llm_exit"` (`trader/runtime/daemon.py`).
Raison : le LLM voit un changement de thèse avant le hard_stop.
`plan_store.close_symbol()` à la clôture.

### 4.3 Sortie via plan armé / resolve (D11)

Les triggers `EXECUTE_ORDER` peuvent inclure des ordres de sortie. Chemin identique
au 4.1 une fois le `TradePlan` créé. La résolution late-binding (D11) s'applique
ici avant tout.

---

## 5. Stops & résolution — `resolve_exit_plan`

`trade_plan.resolve_exit_plan()` (`trader/planning/trade_plan.py`) — résout une **intention
paramétrique** en prix absolu. Types supportés :

| Type `hard_stop` | Description | Résolution |
|---|---|---|
| `price` | Prix absolu | Identité |
| `percent` | % de l'entrée | `entry × percent` ± `min_pct/max_pct` |
| `volatility_multiple` | N × vol de référence | `N × reference_volatility` ± clamp |
| `structural` | Ancre chartiste | Extraction barres fraîches : `swing_low/high/vwap` + buffer |

TP supporte également le type `risk_multiple` (R) : `entry ± R × stop_distance`.

La `reference_volatility` est calculée à partir du cockpit daily ou des barres 15m
(`_reference_volatility_for_symbol`, `trader/runtime/daemon.py`).

La résolution produit un `trace` machine-readable (spec_type, distance, resolved_price,
clamped, reference_volatility…) persisté dans l'événement `armed_plan_resolved`.

**Unification direct/armé (commit `a5c04df`)** : les entrées directes
`OPEN_LONG`/`OPEN_SHORT` passent désormais par le même resolver que les armés.
Seul cas non couvert : un `hard_stop` de type `price` absolu trop serré fourni
directement par le LLM (voir `docs/analysis/comportement-sorties.md` §4).

---

## 6. Univers & rotation (D9 / D10)

### 6.1 Radar (Tier 1 — daily, 0 LLM)

`trader/market/radar.py` — scan daily EOD du pool (`config/pool.yaml`, ~283 symboles).
Score = `efficacité_tendance × force_relative(benchmark_venue) × amplitude`.
La volatilité est **récompensée** (bornée par ATR floor). Séries ajustées obligatoires.

### 6.2 Rotation & hot-sets par venue (D10)

`trader/rotation/venues.py` — une hot-list par place de marché (TW / EU / US).
Classement intra-venue contre son propre benchmark. Recalculé à la clôture de
chaque session.

Univers actif composé à chaque cycle :
`sticky_all ∪ union(hot-lists des marchés OUVERTS à l'instant t)`
*(La `sleeve_24/5` figurait au design D10 mais n'est pas implémentée — forex/commodity retirés, pool 100 % actions.)*

**Sticky** (`sticky_all`) : positions broker + plans armés + exit-watches + ordres
pending — toujours dans l'univers, hors quota, hors logique d'ouverture.

**Hystérésis** : marge Δ (swap seulement si écart d'attractivité ≥ Δ) + dwell K jours
(l'incumbent est protégé K jours). Sortie d'urgence court-circuite K.

Écriture atomique de `universe.yaml` (idempotente — n'écrit que si le contenu
change, jamais vide → fallback dernier univers valide). `rotation/venues.py`

Override LLM : l'agent peut ajouter/retirer des symboles avec raison loggée
(`rotation/override.py`). Tracé dans `rotation/ledger.py`.

---

## 7. Veille / réveils — indicator_watch

`trader/planning/indicator_watch.py` — cube `symbol × indicator × timeframe × op × value`.

Trois modes de déclenchement :

| `on_trigger` | Effet | Contexte |
|---|---|---|
| `WAKE` | Réveille le cycle pour ce symbole | Veille simple |
| `WAKE_WITH_ORDER_INTENT` | Réveille + signale une intention d'ordre | Pré-validation |
| `EXECUTE_ORDER` | Exécute l'ordre SANS re-appel LLM | Plan armé D7B |

**INVARIANT ATOMIQUE** : une seule condition rejetée → toute la veille est rejetée
(jamais de watch amputée). `trader/planning/indicator_watch.py`

TTL max des plans armés : 240 min (aligné sur la revue périodique garantie). `trader/planning/indicator_watch.py`

**exit_watch** : veille attachée à un `TradePlan` ouvert — déclenche `WAKE` quand
une condition technique se réalise post-entrée (ex. `z_score > 1`). Cooldown
configurable (défaut 15 min). `trader/runtime/daemon.py`

**next_wake** : le LLM peut demander son propre réveil via `next_wake_in_minutes`
dans sa décision — il n'est jamais filtré par le gate de pertinence (autonomie
de planification). `trader/runtime/daemon.py`

Opérateurs valides : `>`, `>=`, `<`, `<=`, `==`, `!=`, `abs>`, `abs>=`, `abs<`, `abs<=`.

---

## 8. État persistant — `state/`

| Fichier | Écrit par | Lu par | Contenu |
|---|---|---|---|
| `decisions.jsonl` | `DecisionRecorder` via `decision_ledger_store.append()` | attribution, CLI, cockpit | Une ligne par décision (action, intent, qty, confidence, rationale, executed, reason…) |
| `model_performance.jsonl` | `RuntimeStateWriter.append_model_performance()` via wrapper daemon | `trader/reporting/attribution.py` | Une ligne par fill (entrée + sortie) — base des round-trips |
| `broker.json` | `SimBroker` | `trader/runtime/daemon.py` (reload à chaque cycle) | Positions paper + historique fills |
| `trade_plans.json` | `TradePlanStore` | `trader/planning/exit_engine.py`, `trader/runtime/daemon.py` | Plans ouverts (hard_stop_price, TPs, trailing, watermarks…) |
| `events.jsonl` | `RuntimeStateWriter.append_event()` via wrapper daemon | monitoring / debug | Événements runtime (cycle_started, armed_plan_resolved, watch_triggered…) |
| `history.jsonl` | `RuntimeStateWriter.append_cycle_history()` via wrapper daemon | CLI status | Résumé par cycle (equity, n_executed) |
| `daemon_status.json` | `RuntimeStateWriter.write_status()` via wrapper daemon | cockpit TUI, CLI | Phase courante, PID, decisions_done |
| `current_report.json` | `RuntimeStateWriter.write_current_report()` via wrapper daemon | cockpit TUI | Rapport complet du cycle en cours |
| `learnings.jsonl` | `record_decision()` | `trader/learnings/consolidator.py` | Notes runtime de l'agent (bornées) |
| `learnings_consolidated.json` | `trader/learnings/consolidator.py` | `trader/runtime/daemon.py` (contexte LLM) | Patterns consolidés (≤ seuil bruts → consolidation) |
| `learnings.db` | `learnings_ingest` + daemon (`recalls`) | outil `recall_learnings` | Store SQLite dérivé : notes scorées par outcome (lift/symbole), embeddings, traces de recall |
| `archive/*.jsonl.gz` | `trader/runtime/ledger_rotation.py` (démarrage daemon) | `read_rows_with_archive` (analyses) | Mois passés de decisions/events — rotation mensuelle crash-safe |
| `archive/learnings-*.jsonl` | `RawLearningsStore`/`consolidator` | ingestion recall | Évincés + historique des consolidés — plus rien ne se jette |
| `news_items/YYYY-MM-DD.jsonl` | `market/news_feed` (P1a) | futur analyste-news | Items de news persistés (dédup uuid, purge 60 j) |
| `macro_calendar.json` + `macro_series/` | `macro_calendar`/`macro_series` (P1a) | payload d'attribution | Dates FOMC/CPI + séries macro quotidiennes (DBnomics) |

**Scheduler** (`state/scheduler.json`) : next_wake par symbole, indicator_watches,
stale_streaks. Séparé de `broker.json`.

---

## 9. Intégration LLM / acpx

### 9.1 Transport — `trader/agent/llm.py`

`AcpxBackend` (`trader/agent/llm.py`) : invoque `acpx --format quiet --allowed-tools "" --no-terminal --non-interactive-permissions deny --model <m> exec [prompt]`.

Prompt labellisé `[casys-trader:runtime-brain]` pour nommage des sessions acpx.
Session jetable (`exec`) — pas d'état partagé entre cycles.

`LlmRouter` cascade sur `OpenAICompatibleBackend` (Ollama cloud) si `acpx` échoue
avec un code retryable (rate-limit, quota). `trader/agent/llm.py`

Modèle courant : `gpt-5.5/medium` (Codex Spark, faible latence). `trader/agent/llm.py`

**Fork acpx (2026-07-02)** : le runtime et le consolidateur pointent vers le
fork `Casys-AI/acpx` (branche `casys-patches`) via `TRADER_ACPX_BIN` /
`TRADER_CONSOLIDATOR_ACPX_BIN` (`.env`) — 2 patchs : erreurs quiet structurées
sur stderr (fini les exit=1 muets) et bridges jamais orphelins (shutdown
bridge-first sur signal). Le canon npm reste le binaire global de la machine.
Rebuild après rebase : `make fork-acpx-build`. Les 2 branches `fix/*` du fork
sont prêtes pour des PRs upstream.

### 9.2 Reap des ponts orphelins — `_reap_orphan_bridges`

`trader/agent/llm.py` — les processus `codex-acp` (bridge acpx↔codex) démarrés via `setsid`
échappent au `killpg` et survivent à la fin de l'appel. Fix : snapshot `ps` avant
l'appel, diff après, SIGKILL sur les PIDs nouveaux dont le `cwd` correspond au
projet. Déclenché via `_run_one_shot_command.finally`. Cf post-incident
`memory/casys-trader-acpx-bridge-pileup.md`.

### 9.3 `trader/agent/client.py` — façade transport, protocole séparé

`trader/agent/client.py` reste la façade transport. L'import historique
`trader.codex_client` est un alias de compatibilité. Les contrats et
parseurs vivent désormais dans `trader/agent_protocol/` :

- `types.py` : `Decision`, `IndicatorRequest`, `ContextResearchRequest`,
  `BatchToolCallRequest` ;
- `prompts.py` : contrats de sortie, vocabulaire watches/tools, builders ;
- `parsing.py` : extraction JSON, parsing décision/batch/tool calls.

La réponse texte du LLM est traduite en `Decision` (dataclass frozen).
Fail-safe : toute erreur (timeout, JSON invalide, clé manquante) → `Decision.hold(sym, reason)`.
Le LLM ne trade jamais sur une réponse douteuse.

La `Decision` inclut : `action`, `quantity`, `confidence`, `rationale`, `intent`,
`exit_plan`, `indicator_watch`, `cancel_watch_ids`, `next_wake_in_minutes`, `learning`.

---

## 10. Outils domaine — la tournée d'outils du LLM (2026-07-02)

Design : `docs/superpowers/specs/2026-06-29-agent-domain-tools-design.md`.
Flag : `CASYS_AGENT_TOOLS_ENABLED=1` (actif ; visible au startup dans le log
`[config] … agent_tools=True`).

Le package `trader/agent_tools/` contient le core d'exécution bornée, les
handlers par domaine et `registry.py`. `trader.agent_tools.__init__` réexporte
seulement l'API publique d'exécution ; les validateurs/handlers privés restent
dans leurs modules propriétaires.

Le LLM reçoit UN prompt et peut répondre soit le contrat final, soit
`{"tool_calls": [...]}` — UNE tournée max, puis décision finale obligatoire
(sinon HOLD `tool_loop_blocked`). Les outils ne passent PAS par acpx
(`--allowed-tools` reste `""`) : le daemon parse, valide contre
`agent_tools.TOOL_REGISTRY` (allowlist Python) et exécute — lecture seule.

Registre (9) : `get_freshness`, `get_active_plans`, `get_position_risk`,
`get_attribution`, `get_recent_decisions`, `get_indicator_context`
(voie moderne de REQUEST_CONTEXT), `describe_data`, `find_indicators`
(découverte du cube sémantique TraderNexus), `recall_learnings` (mémoire, §11).

Garde-fous : budgets 24 appels/lot et 3/symbole, cap 32 calls sérialisés
(sentinel `truncated`), args scrubbed (profondeur/longueur), contexte borné au
chunk (pas de fuite inter-chunks), budget modèle réservé (2 appels/chunk en
mode tournée). Tout est tracé dans `runtime.tool_calls` du ledger → dérivable
par `tool_trace.summarize_tools` et `tool_usage` (section `domain_usage`).

Outils d'ACTION (set_next_wake, propose_indicator_watch, cancel_watch,
record_learning, puis propose_order) : Phases 4-5, non implémentées — notes de
design en mémoire projet.

## 10.1 Logging et dépendances du refactor

Le refactor n'ajoute aucune dépendance dans `pyproject.toml` ou `uv.lock`.
Le choix final reste donc :

- `logging` standard library comme API de logging ;
- `trader/runtime/logging_setup.py` comme point d'installation ;
- `RichHandler` seulement en TTY, `StreamHandler` texte en non-TTY ;
- `markup=False` pour éviter les balises Rich dans les logs machine ;
- loggers `trader` et `casys-trader` avec `propagate=False` ;
- bruit `ib_async` abaissé à `CRITICAL`.

Les modules extraits utilisent `logging.getLogger(__name__)` pour les logs de
frontière (`planner_batch`, `market_snapshot`, `decision_recorder`). Aucun
`loguru`, `structlog`, `dependency-injector`, `pydantic` ou `attrs` n'a été ajouté :
les `dataclasses`, callbacks explicites et wrappers de compatibilité suffisent
pour cette tranche.

## 11. Mémoire outcome-weighted — recall des learnings (2026-07-02)

Design : `docs/superpowers/specs/2026-07-02-learnings-recall-design.md`.

Chaîne : les learnings ne se jettent plus (archives §8) → `learnings_ingest`
construit `state/learnings.db` (SQLite dérivé, reconstructible : notes +
facettes + FTS5 + embeddings OpenAI pré-calculés) → scoring FLAIR normalisé
par symbole (lift vs base rate + shrinkage bayésien ; verdicts issus du
forward via `decision_quality`) → l'outil `recall_learnings{symbol?|family?|
query?}` sert un hybride facettes → FTS5+cosine → RRF → outcome×decay
(~2-8 ms local, +~3 s max si embed de query OpenAI, dégradation FTS sinon).

La table `recalls` trace quelles notes ont servi quelle décision
(note_ids × decision_id) — c'est le flux qui alimentera MemRL (Q-value) et le
bench A/B `decision_bench`. Le push linéaire des 15 slots consolidés reste en
place tant que la mesure n'a pas tranché.

## 12. Gestion des données — rotation et archives (2026-07-02)

Doc : `docs/specs/2026-07-02-agent-data-lifecycle.md`.

- **Rotation mensuelle** (`ledger_rotation`, au démarrage du daemon) :
  decisions/events des mois passés → `state/archive/<stem>-YYYY-MM.jsonl.gz`.
  Crash-safe : réécriture atomique de l'archive (fusion + dédup decision_id +
  validation par relecture) avant `os.replace` du vif ; archive corrompue →
  abort, vif intact. Les analyses lisent archives+vif via
  `read_rows_with_archive` ; les lecteurs runtime (tail TUI) restent sur le vif.
- **Caches** : dédup d'append du ledger en mémoire (fini les ~90 Go d'IO/j),
  `decision_audit.json` par mtime.
- **Purges** : radar_cache > 30 j (tick rotation), news_items > 60 j (P1a).
- Sessions acpx (~/.acpx, 1,3 Go) : traité par patch de rétention NATIVE dans
  le fork (backlog), pas de prune côté casys.

## 13. Collecte macro/news — P1a (en livraison 2026-07-02)

Spec : `docs/superpowers/specs/2026-07-02-macro-analyste-news-spec.md` ;
sources : `docs/specs/2026-07-02-macro-data-sources.md`.
Items de news persistés + calendrier FOMC/CPI (`macro_next` par décision) +
séries macro quotidiennes via DBnomics (zéro clé). Tout attribution-first ;
l'analyste-news (LLM offline, brief quotidien borné) viendra quand le stock
aura ~2-3 semaines (P2), puis exposition en pull `get_macro_brief` (P3).

---

*Doutes / points à valider humainement listés dans le résumé de livraison.*
