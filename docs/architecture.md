# Architecture — casys-trader

> Ce document décrit l'architecture technique du daemon de trading paper piloté par LLM.
> **Généré par analyse statique du code — à re-vérifier si l'architecture évolue.**
>
> **⚡ Migration CLEAN ARCHITECTURE livrée le 2026-07-08** (~32 commits). La structure est alignée sur :
> - **`domain/`** = cœur PUR (zéro I/O) : types (Pydantic + dataclasses) + calculs. Packages :
>   `domain/market/{features,regime,family_regime,volatility,fx,sessions,execution_eligibility,gross_priority}`,
>   `domain/planning/{exit_engine,exit_plan_spec,relevance_gate,scheduling,watches}`, `domain/learnings/scoring`,
>   `domain/{trade_plan,contracts,orders,decisions,risk,strategy_language,llm,market_data,semantic}`.
> - **`application/`** = use-cases, 6 sous-modules `decide/ execute/ exit/ cycle/ record/ migration/` (plus de fichiers plats).
> - **`infrastructure/`** = adaptateurs I/O : `state_db/` (SQLite + `sim_broker`, `scheduler_json`, `learnings_store`),
>   `market_sources/` (data_source, ib_source, fx_rates, macro_series, news_feed, radar_data, market_data_yf),
>   `llm/` (acpx_backend, openai_backend), `queue/`.
> - **`interfaces/`** UI · **`runtime/`** composition root (daemon allégé, `run_cycle` découpé).
>
> Les anciens modules `market/`, `planning/`, `agent/llm.py`, `execution/broker.py` sont désormais des **FAÇADES**
> ré-exportant depuis `domain/`/`infrastructure/` (importeurs inchangés). Ports = `Protocol` regroupés par module dans
> `<module>/protocols.py` (convention ; plus de `ports.py`) ; `trader/reporting/*/protocols.py` garde les ports
> reporting colocalisés avec les moteurs/read models concernés. SQLite = source unique (plus de shadow JSON).
> **Inversions de dépendance toutes cassées** (dépendances vers l'intérieur ; domaine pur vérifié par test_package_layout).
>
> Refactor modulaire en place sur `main` par tranches compatibles.
> Les tranches récentes sont suivies dans `docs/superpowers/plans/`
> (`tools-market-data-boundary`, `tools-news-feed-boundary`,
> `tools-portfolio-boundary`, `tools-memory-boundary`,
> `interfaces-boundary`, `infrastructure-boundary`,
> `flat-compat-facades`, `domain-semantic-boundary`,
> `planning-scheduling-boundary`, `agent-learnings-boundary`,
> `market-rotation-boundary`, `wake-watch-runtime-glue`,
> `learnings-selection-readmodel`, `planned-exits-boundary`,
> `decision-reason-domain`, `tool-usage-readmodel`,
> `execute-queue-dispatch`, `execute-queue-plan-payload`,
> `fill-outcome-accounting`, `fill-plan-effects`,
> `cycle-finalization`, `queue-runtime-bootstrap`,
> `data-source-runtime`, `market-rotation-runtime`,
> `daemon-bootstrap`, `cycle-dispatch`, `cycle-reporting`,
> `runtime-shutdown`).

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
| `trader/application/exit/exit_update.py` | Application fail-safe des mises à jour de plan ouvert compilées depuis `strategy_exit` | `ExitUpdateResult` décrit l'application persistante ; `validate_exit_update` est le dry-run pur utilisé par le worker |
| `trader/application/execute/cycle_decision.py` | Application d'une décision symbole pendant un cycle | contient `execute_one_cycle_decision`, `DecisionExecutionContext` et `DecisionExecutionState` ; le daemon réexporte `_execute_one_cycle_decision` comme alias de compatibilité |
| `trader/application/exit/armed_plans.py` | Résolution applicative des triggers `EXECUTE_ORDER` en décisions armées ou réveils planificateur | contrats `Protocol` locaux pour position/volatilité ; le daemon conserve logs et événements |
| `trader/application/decide/planner_batch.py` | Batch LLM, budget modèle, tournée d'outils, REQUEST_CONTEXT | appelé via `daemon._batch_decide()` |
| `trader/application/decide/protocols.py` | Contrat local `DecisionBatchPlanner` des chemins batch/queue | runtime injecte l'implémentation `agent.client`; prompts/parsing restent dans `agent/` |
| `trader/application/decide/context_projection.py` | Projection queue `decision_focus_v1` : cible/risque/recherche locale, radar borné et résumés pull-ready | appliquée par `queue_dispatch` sans muter le snapshot de cycle |
| `trader/application/cycle/market_snapshot.py` | Barres runtime/daily/exit, fraîcheur, FX, eligibility, tradable maps | retourne `MarketSnapshot`, le daemon l'unpack |
| `trader/application/record/decision_entries.py` | Construction pure des entrées décision runtime avant persistance | source `llm`/`infra`/`armed_plan`, traces d'outils et raisons HOLD sans side effects |
| `trader/application/record/decision_ledger_rows.py` | Projection pure d'une entrée décision et du rapport de cycle vers la ligne d'audit versionnée | aucun I/O ; le port d'append reste colocalisé dans `decision_recorder.py` |
| `trader/application/record/decision_watches.py` | Préparation pure des `indicator_watch` demandées par une décision | le daemon garde logging et application scheduler |
| `trader/application/record/decision_recorder.py` | Enrichissement décision, ledger, report, status, event, recall traces | l'écriture humaine `agent_trace.log` est injectée par `runtime/agent_trace_runtime.py` |
| `trader/application/cycle/schedule.py` | Politique applicative de réveil : bornes explicites, backoff stale, due symbols, veilles et événements de réveil | le daemon garde des wrappers privés de compatibilité |
| `trader/agent/protocol/strategy_language.py` | Compilateur du langage agent Pine-like canonique (`strategy_entry`/`strategy_exit`/`strategy_close`) | `parsing.py` consomme ses primitives ; les anciens action tools sont rejetés |
| `trader/application/record/confidence_feedback.py` | Feedback persistant des rejets de gate confiance vers les learnings de l'agent | le daemon conserve le wrapper privé historique |
| `trader/application/execution_eligibility.py` | Classification execution/planning par symbole et raison de blocage d'exécution | utilisé par `market_snapshot` et wrappers privés du daemon |
| `trader/application/execute/entry_context.py` | Construction pure du snapshot durable `entry_context` attaché aux TradePlans | utilisé par le chemin direct post-fill et le payload execute queue |
| `trader/application/execute/queue_dispatch.py` | Producteur/collecteur applicatif des tâches `execute_order` en mode queue : payload, dedup, polling, décodage fill et raisons fail-closed | le daemon garde l'enregistrement de décision |
| `trader/application/execute/queue_plan.py` | Préparation pure du payload atomique `plan_to_upsert` / `symbol_to_close` pour le mode execute queue | contrat `Protocol` local pour lire les plans ouverts ; le daemon fournit le contexte runtime |
| `trader/application/exit/exit_bars.py` | Fetch/validation des barres fines de sortie et calcul high/low de fenêtre pour plans ouverts | branché comme `exit_bars_fetcher` dans `market_snapshot`, wrappers privés du daemon |
| `trader/application/execute/fill_outcome.py` | Accounting post-fill des décisions exécutées : payload `model_performance`, raison de sortie LLM, champs commission/fx de l'entrée décision | le daemon garde le snapshot portefeuille et l'écriture durable |
| `trader/application/exit/fill_plan_effects.py` | Effets post-fill sur les plans : close/sync des sorties, création/snapshot des plans d'ouverture, SCALE_IN et FLIP | le daemon fournit contexte runtime et conserve le scheduling post-entry |
| `trader/application/record/gross_feedback.py` | Feedback applicatif des ouvertures rejetées par le plafond gross exposure | le daemon garde un wrapper public historique |
| `trader/application/cycle/decision_scope.py` | Use case de préparation du scope décisionnel : éligibilité analyse, résolution des plans armés, quiet gate et couverture des barres runtime/daily | dépendances injectées par contrats locaux ; le daemon garde broker, logs, events et recorder |
| `trader/application/cycle/infra_holds.py` | Construction applicative des HOLD infra (`quiet_gate`, `stale_market_data`) sans appel modèle | contrats `Protocol` locaux pour wakes/clamp session ; le daemon garde scheduler, log et persistance |
| `trader/application/learnings_recall.py` | Provider applicatif de recall mémoire : cache embeddings, timeout court, fallback FTS | contrats `Protocol` pour store et embedder |
| `trader/application/execute/order_admission.py` | Helpers purs d'admission : intent, résolution position-aware, clamp sortie, stop, risk metrics | réutilisé par `risk_admission`, `planned_exits` et les payloads queue |
| `trader/application/execute/risk_admission.py` | Admission risque : sizing `risk_pct`, métriques d'entrée, plafond par trade, gate de confiance et `RiskGate.check()` final | contrats `Protocol` locaux pour le gate ; le daemon garde logging, recorder, broker et scheduling |
| `trader/application/exit/planned_exits.py` | Exécution applicative déterministe des sorties planifiées : évaluation des plans ouverts, clamp sortie, garde d'exécution, mutation broker/plan-store et payload performance | le daemon conserve `_apply_planned_exits()` et `_plan_snapshot()` comme wrappers privés |
| `trader/application/record/plan_review.py` | Persistance et réinjection du dernier verdict LLM sur les plans ouverts | le daemon conserve les wrappers privés historiques |
| `trader/application/reference_volatility.py` | Calcul de volatilité de référence pour résoudre stops/trailings en multiples de volatilité | le daemon conserve les wrappers privés monkeypatchables |
| `trader/application/execute/risk_capacity.py` | Contexte de capacité exposé à l'agent : gross exposure, plafonds buy/sell, quantités natives FX-aware | le daemon injecte le broker, les prix, les FX et la fonction devise |
| `trader/application/execute/fee_estimate.py` | Projection d'un modèle de commission en coût aller-retour et seuil de rentabilité décisionnel | contrat local `CommissionCalculator` ; aucune dépendance vers un broker concret |
| `trader/application/execute/protocols.py` | Ports `Broker` et `CommissionModel` requis par les cas d'usage d'exécution | implémentés par les adaptateurs paper ; `execution.protocols` garde les alias publics historiques |
| `trader/application/portfolio/snapshot.py` | Assemblage de la valorisation live depuis un lecteur de positions, les prix et les taux FX injectés | port local `PortfolioReader`; `execution.portfolio` conserve la façade historique |
| `trader/application/record/tool_outcomes.py` | Finalisation des outcomes réels des action tools avant persistance des décisions | `reporting.tool_trace` réexporte l'ancien point de compatibilité |
| `trader/application/cycle/watch_scanner.py` | Scan applicatif des indicator/exit watches : fetch des barres, évaluation, cooldown, retrait et réveil symbole | le daemon conserve l'émission d'événements/logs runtime |
| `trader/application/universe/activation.py` | Revalidation de la sélection agent complète contre le scope, les sticky et le quota actifs | `market.rotation.venues` reste l'adaptateur d'activation fail-safe et réexporte temporairement la fonction |
| `trader/application/universe/scope_rotation.py` | Transitions applicatives clôture → parent quantitatif → enfant pré-open immuable | le sélecteur de challengers news est un `Protocol` colocalisé ; `market.rotation.venues` branche le callback et garde la façade historique |
| `trader/application/queue/contracts.py` | Contrat applicatif de requeue transitoire (`RetryableError`) | le worker durable l'importe et le réexporte ; la décision symbole ne dépend plus de l'infrastructure queue |
| `trader/agent/` | Contexte agent, analyste macro/news, agent univers, mémoire mandat/stratégie, mémoire learnings/RAG, façade planner, transport LLM/acpx | compat virtuelle : `trader.agent_context`, `trader.codex_client`, `trader.llm`, `trader.tools.memory.Memory`, `trader.tools.memory.LearningsStore`, `trader.learnings.*`, `trader.learnings_store`, `trader.embeddings`, `trader.consolidator` |
| `trader/agent/protocol/` | Types, prompts, parsing du contrat LLM | utilisé par `trader/agent/client.py` |
| `trader/agent/tools/` | Package des outils domaine lecture seule | `registry.TOOL_REGISTRY` assemble les 7 outils read-only exposés au LLM |
| `trader/agent/learnings/` | Buffer brut JSONL, sélection pure, store SQLite recall, embeddings, consolidateur | mémoire machine de l'agent ; `trader.learnings.*` reste virtuel |
| `trader/domain/` | Primitives neutres (`Bar`, `MarketError`, `Side`), identité durable des décisions, vocabulaire partagé des `decision_reason_code` et catalogue sémantique gouverné (`domain/semantic/`) | évite que `market`/`planning`/`agent` importent `tools` ou `reporting` pour accéder à un vocabulaire métier |
| `trader/domain/decision_identity.py` | Construction de l'identité stable `cycle_ts|sequence|symbol` partagée par ledger, plans et learnings | pure, sans connaissance du format JSONL ni du runtime |
| `trader/domain/llm.py` | Valeurs `LlmCompletion`/`LlmFailure`, port `LlmBackend` et politique pure de fallback `LlmRouter` | `agent.llm` conserve la factory d'environnement et réexporte le contrat historique |
| `trader/domain/execution/risk_gate.py` | Politique pure de dernière barrière : sizing maximal, gate de confiance et validation des bornes d'exposition | `execution.risk` reste une façade de compatibilité et conserve seulement le branchement config historique |
| `trader/domain/execution/fill_accounting.py` | Comptabilité pure d'un fill : nouvelle position, prix moyen et débit cash USD frais inclus | consommée directement par les adaptateurs paper SQLite/JSON ; `execution.commission` garde l'alias historique |
| `trader/domain/universe/selection.py` | Politiques pures de hotlist : hystérésis, sorties d'urgence, sticky hors quota, override borné et composition de l'univers actif | `trader.market.rotation` conserve la façade historique |
| `trader/domain/portfolio/snapshot.py` | Valeurs et agrégats purs du portefeuille live : équité, PnL, expositions et contexte agent | aucune dépendance vers un broker ou une source de prix concrète |
| `trader/domain/universe/candidate_scope.py` | Composition pure du pool top radar + challengers news, rétention TTL et références des runs candidats | consommé par l'adaptateur `market/rotation/venues.py` |
| `trader/domain/universe/user_overrides.py` | Modèle et politique purs pin/ban/sticky de l'univers opérateur | la lecture/écriture YAML vit dans l'adaptateur filesystem ; `market.rotation.user_overrides` reste une façade |
| `trader/planning/` | Plans de trade, scheduler de réveils, veilles, exit engine, gate de pertinence | compat : `trader.trade_plan`, `trader.indicator_watch`, `trader.exit_engine`, `trader.relevance_gate`, `trader.scheduling.scheduler`, `trader.tools.scheduler` |
| `trader/execution/` | Contrats `Order`/`Fill`, ports `Broker`/`CommissionModel` et façades publiques historiques | politiques dans `domain/execution`, cas d'usage dans `application/execute`, adaptateurs dans `infrastructure`; compat virtuelle : `trader.tools.execution`, `trader.tools.portfolio`, `trader.risk` |
| `trader/market/` | Port `DataSource`, adaptateurs yfinance/IB/composite, fraîcheur, indicateurs, FX, news, macro, radar, régime, priorisation gross exposure | port : `trader.market.protocols.DataSource`; compat virtuelle : `trader.tools.market`, `trader.tools.data_source`, `trader.tools.ib_source`, `trader.tools.news_feed`, `trader.fx`, `trader.features`, etc. |
| `trader/infrastructure/queue/` | File de tâches durable, workers, pools, backpressure | backend technique utilisé par le runtime queue-on ; compat virtuelle : `trader.queue.*` |
| `trader/infrastructure/queue/order_handler.py` | Adaptateur payload queue → `Order`/`TradePlan` → unit of work SQLite atomique | owner canonique importé directement par la composition runtime ; aucune dépendance application → infrastructure |
| `trader/infrastructure/brokers/commission_models.py` | Adaptateurs de tarification paper (`none`, approximation IBKR) et résolution du modèle configuré | les contrats restent dans le domaine ; `execution.commission` et `execution.broker` réexportent les noms historiques |
| `trader/infrastructure/files/` | Adaptateurs atomiques pour `universe.yaml`, `venue_state.json`, le ledger décision JSONL et la rétention du cache radar | verrouillage `flock`, YAML/JSON/tempfiles et purge restent hors domaine/application |
| `trader/infrastructure/files/decision_ledger.py` | Store append-only `decisions.jsonl`, déduplication, remplacement atomique et migrations opérateur seed/backfill | implémente les ports consommateurs de l'application ; importe la projection pure, jamais l'inverse |
| `trader/infrastructure/state_db/` | Backend SQLite canonique de l'état paper, broker store, outbox et stores append-only/projections de briefs, scopes candidats et runs univers | `state/casys.db` est la source durable sans flag de backend ; les stores JSONL de situation/univers restent indépendants du backend paper ; compat virtuelle : `trader.state_db.*` |
| `trader/market/rotation/` | Orchestration/façades de rotation par venue : scheduling, activation pré-open et ledger | politiques dans `domain/universe`, transitions dans `application/universe`, I/O dans `infrastructure/files` ; compat virtuelle : `trader.rotation.*`, `trader.rotation_*` |
| `trader/support/` | Helpers support stables : config (`pool`, `portfolio`), metadata git/code version, process env | compat virtuelle : `trader.config.*`, `trader.metadata.*`, `trader.system.*` |
| `trader/support/coercion.py` | Coercition sans dépendance des nombres finis et listes de dictionnaires | partagée par reporting et cockpit ; `runtime_state` conserve les deux aliases privés historiques |
| `trader/reporting/` | Façades attribution/audit/bench/ledger/stats/tool usage/meta-performance, read models et renderers | analyse/rendu ex-post et lecture seule ; `reporting.decision_ledger` et `reporting.ledger.*` gardent uniquement la compatibilité vers les owners domain/application/infrastructure ; les ports du ledger vivent auprès de leurs consommateurs application |
| `trader/interfaces/cli/` | Entry points CLI canoniques (`stats`, `attribution`, `tool_usage`, `tui`) | compat virtuelle : `python -m trader.commands.stats`, `python -m trader.stats`, etc. |
| `trader/runtime/cycle_scheduling.py` | Adaptateur runtime wake/watch : délègue la politique à `application/cycle/schedule.py` et `application/cycle/watch_scanner.py`, puis émet events/logs et compat wrappers | évite que `daemon.py` réimporte directement la glue applicative |
| `trader/runtime/cycle_dispatch.py` | Adaptateur runtime d'appel `run_cycle()` : porte le paquet de paramètres CLI/env/queue/consolidation et le forwarde depuis `daemon.main()` | évite deux appels `run_cycle(...)` dupliqués dans `main()` et garde le contrat runtime testable |
| `trader/runtime/decision_dispatch_runtime.py` | Adaptateur de sélection batch/queue, construction des faits queue et streaming des décisions réductrices vers l'exécuteur injecté | garde les stores/ledgers opaques, ne dépend pas de l'infrastructure et laisse les use cases de décision sous `application/decide/` |
| `trader/runtime/cycle_reporting.py` | Adaptateur runtime de persistance post-cycle : `last_report.json` + `history.jsonl`, avec règle no-due active-only | garde les writes de reporting hors de la boucle `main()` et centralise la condition "cycle actif" |
| `trader/runtime/cycle_finalization.py` | Adaptateur runtime de fin de cycle : consolidation learnings, collecte macro best-effort et cache feedback gross | garde les side effects mémoire hors du coeur décisionnel ; contrats `Protocol` locaux pour les dépendances injectées |
| `trader/runtime/daemon_bootstrap.py` | Adaptateur runtime de démarrage : rotation mensuelle des ledgers, bootstrap du backend état, chargement cash initial et construction scheduler | garde les side effects de boot hors de `daemon.main()` avec factories injectées pour préserver les tests runtime |
| `trader/runtime/agent_cycle_context.py` | Projection bornée des plans et assemblage du contexte agent d'un cycle | concentre les lectures KPI/learnings/régime et garde `daemon.run_cycle()` sur l'orchestration ; les anciens helpers daemon restent des alias |
| `trader/runtime/cycle_process_state.py` | État mutable inter-cycle strictement local au process (`last_llm_at`, feedback gross) | explicite le reset au restart et évite les globals métier dispersés dans le daemon |
| `trader/runtime/queue_runtime.py` | Bootstrap runtime des pools `decide`/`execute_order` : flags, ledgers, pools, handlers et stack SQLite partagée | garde la queue canonique hors du bloc `main()` tout en laissant `run_cycle()` choisir le chemin queue/synchrone |
| `trader/runtime/data_source_runtime.py` | Bootstrap et transitions runtime des sources de données : profil composite/direct, IB obligatoire ou dégradé paper, lazy attach et détachement sur échec connexion | garde l'orchestration réseau/adapter hors de `daemon.main()` avec dépendances injectées pour préserver les tests runtime |
| `trader/runtime/market_rotation_runtime.py` | Adaptateur runtime du tick D10/D15 : charge `radar.yaml`, persiste les parents close et enfants finaux pré-open, relit les projections univers exactes, injecte le snapshot `last_regime` typé et appelle `market.rotation.venues.tick()` en fail-safe | l'ancien override synchrone reste une compatibilité ; le chemin prod prépare à T-90 et active à T-15 |
| `trader/runtime/news_macro_runtime.py` | Runner async single-flight de briefs macro/news par venue et `candidate_scope_id` | best-effort, kill switch dédié, aucun blocage du cycle |
| `trader/runtime/universe_intelligence_runtime.py` | Runner async coalescent de sélection hotlist par venue, projection bornée du brief et écriture d'une préparation exacte par scope | l'activation reste synchrone et déterministe au pré-open ; les erreurs sont observables et retombent sur la baseline |
| `trader/runtime/runtime_shutdown.py` | Adaptateur runtime de shutdown best-effort : arrêt pools queue, disconnect data source, release pid file | garde le `finally` de `daemon.main()` court et préserve la règle "ne jamais bloquer la sortie" |
| `trader/runtime/` | Daemon, CLI, logging, PID file, IB attach, rotation ledger, writers d'état fichier | compat virtuelle : `python -m trader.daemon`, `python -m trader.cli` |
| `trader/reporting/audit/decision_quality.py` | Moteur d'audit ex-post des décisions depuis ledger + prix forward | `reporting.decision_audit` reste la façade historique ; `decision_bench`, `runtime.cli` et `read_models.meta_performance` lisent le moteur canonique |
| `trader/reporting/bench/decision_bench.py` | Moteur de bench contrefactuel de modèles sur les décisions auditées | `reporting.decision_bench` reste la façade historique ; `runtime.cli` lit le moteur canonique |
| `trader/reporting/ledger/decision_ledger.py` | Façade de compatibilité de l'ancien write-side | réexporte exactement l'identité domaine, le builder application et l'adaptateur filesystem ; aucun I/O canonique sous reporting |
| `trader/reporting/read_models/live_kpis.py` | Projection live des KPI depuis `state/` pour daemon/cockpit/TUI | `reporting.stats` rend les KPI ; `interfaces.cli.stats` possède la CLI |
| `trader/reporting/read_models/trade_history.py` | Reconstruction canonique des round-trips depuis `model_performance.jsonl` et filtres de régime partagés | consommé par attribution et diagnostics hard-stop ; `attribution.compute_round_trips` reste un alias historique |
| `trader/reporting/read_models/attribution.py` | Agrégation ex-post des round-trips par confidence et raison de sortie | `reporting.attribution` rend le rapport ; le daemon et le consolidateur lisent ce read model canonique |
| `trader/reporting/read_models/hard_stop_diagnostics.py` | Diagnostic contrefactuel prix-only des sorties hard-stop après application des filtres partagés | `attribution.compute_hard_stop_diagnostics` et `select_hard_stop_symbols` restent des aliases historiques |
| `trader/reporting/read_models/meta_performance.py` | Projection compacte de `decision_audit.json` pour contexte agent/consolidateur | `reporting.meta_performance` reste la façade de compatibilité ; le daemon et le consolidateur lisent ce read model canonique |
| `trader/reporting/read_models/universe_pipeline.py` | Projection opérateur tolérante du pipeline univers (scope, scout, brief, agent, activation) par venue | lit les projections `state/` sans coupler le cockpit aux formats de persistance ; `runtime_state` réexporte l'ancien helper privé |
| `trader/reporting/read_models/runtime_state.py` | Assemblage global des read models et fichiers `state/` pour TUI/cockpit | compat virtuelle : `trader.read_models.*` ; délègue la projection univers au module canonique dédié |
| `trader/reporting/read_models/tool_usage.py` | Projection ex-post des traces d'outils et de leur qualité forward depuis le ledger décision | `reporting.tool_usage` rend le rapport ; `interfaces.cli.tool_usage` possède la CLI |
| `trader/reporting/renderers/` | Rendus opérateur des projections reporting (`live_kpis`, `tool_usage`) | `reporting.stats` et `reporting.tool_usage` restent les façades historiques ; les CLI importent projection + renderer canoniques |
| `trader/interfaces/cockpit/projections/` | Projections UI pures et tolérantes, sans Rich/Textual, consommées par les pages cockpit | la page univers réexporte temporairement sa projection historique et ne recalcule plus les lignes dans le renderer DataTable |
| `trader/interfaces/cockpit/projections/decisions.py` | Projection du ledger cockpit : filtres/comptages/groupement et cellules sémantiques prêtes au rendu | la page décisions garde Rich/Textual et réexporte ses helpers historiques ; les filtres canoniques publics vivent dans `reporting/read_models/decision_filters.py` |
| `trader/interfaces/cockpit/projections/portfolio.py` | Projection typée des positions, stops et agrégats gross/net/P&L du cockpit | la page portfolio ne refait plus les calculs dans le widget et réexporte les helpers historiques |
| `trader/interfaces/cockpit/projections/plans.py` | Projections typées du playbook : exit plans ordonnés, ordres armés, watches actives et exit watches | les rendus complet/compact et leurs compteurs partagent les mêmes projections ; la page réexporte les helpers historiques |
| `trader/interfaces/cockpit/projections/health.py` | Projections typées de toute la page Health : fraîcheur, FX, sources, LLM, learnings et résumé univers | les projections restent sans Rich/Textual ; la configuration IB est fournie explicitement par l'adaptateur de page |
| `trader/interfaces/cockpit/renderers/` | Rendus Rich explicites des projections cockpit, dont le meter de confiance | `cockpit/format.py` reste une bibliothèque sémantique sans import Rich et conserve un wrapper paresseux de compatibilité |
| `trader/interfaces/cockpit/` | App Textual, événements cockpit, supervisor local | `trader.cockpit` reste runnable via compat virtuelle |
| `trader/interfaces/ui/` | Builders Rich purs, TUI textuelle, palette | `trader.ui.*` et `trader.tui` restent des façades import/CLI legacy virtuelles |
| `trader/__init__.py` | Finder de compatibilité import/CLI | `trader.tools.*`, `trader.commands.*`, `trader.cockpit.*`, `trader.ui.*`, `trader.daemon`, `trader.cli`, `trader.stats`, `trader.attribution`, `trader.tool_usage` et `trader.tui` sont virtuels |

### 1.2 Niveaux d'architecture

Le répertoire `trader/` reste volontairement peu profond pendant la migration,
mais ses packages ne sont pas tous du même niveau :

| Niveau | Packages | Règle pratique |
|---|---|---|
| Composition runtime | `runtime/`, `interfaces/cli/`, adaptateurs transitoires `market/rotation/{core,venues,user_overrides}.py`, alias legacy `trader.daemon`/`trader.cli` | peut assembler les dépendances et déclencher les side effects ; les trois adaptateurs rotation ne peuvent viser que `infrastructure/files` |
| Services applicatifs | `application/` | orchestre un cas d'usage testable sans être l'entrypoint process |
| Infrastructure technique | `infrastructure/files/`, `infrastructure/queue/`, `infrastructure/state_db/` | backends durables et mécaniques ; pas de logique de décision métier |
| Capacités métier | `market/`, `planning/`, `execution/`, modules purs de `market/rotation/`, `agent/` (`protocol/`, `tools/`, `learnings/`) | porte la logique du domaine et ne dépend pas de `runtime/` ni de l'infrastructure |
| Primitives transverses | `domain/`, `domain/semantic/`, `support/` | types/catalogues/helpers stables, sans dépendance montante |
| Read models et surfaces | `reporting/`, `reporting/read_models/`, `interfaces/ui/`, `interfaces/cockpit/` | lit l'état produit par le runtime, ne décide pas à sa place |
| Compatibilité legacy | alias virtuels de `trader/__init__.py` | délègue vers le canonique ; aucun nouvel import interne ne doit viser ici |

La cible n'est donc pas de basculer d'un coup vers une arborescence générique
`core/infra/apps`. Le travail en cours est d'abord de rendre le niveau de chaque
module explicite, puis de réduire les anciennes façades à une couche de
compatibilité virtuelle. `infrastructure/` existe uniquement pour les backends
techniques qui encombraient la racine (`queue`, `state_db`).

Le choix volontaire reste de ne pas frameworkiser en `ports/`/`adapters`
génériques. En revanche, deux packages neutres existent maintenant parce qu'ils
suppriment des cycles réels :

- `domain/` porte les primitives stables et le catalogue sémantique partagé par
  `market`, `planning`, `agent` et les façades legacy ;
- `support/` porte les helpers partagés de configuration, version git et processus.

Quand une frontière n'est utile qu'à un cas d'usage, le contrat reste local sous
forme de `Protocol` dans le module applicatif plutôt que de créer un dossier
`ports/` dédié. Les packages `ports` existants restent réservés aux contrats
partagés et déjà structurants (`execution`, `market`).

Les anciens imports restent compatibles quand ils existaient déjà
(`trader.tools.market.Bar`, `trader.tools.execution.Order`, `trader.tools.scheduler.Scheduler`,
`trader.runtime.code_version`, `trader.config.pool`, `trader.metadata.code_version`,
`trader.process_env`, `trader.read_models.runtime_state`, `trader.stats`,
`trader.agent_protocol.parsing`, `trader.agent_tools.registry`,
`trader.daemon`, `trader.cli`, `trader.attribution`, `trader.tool_usage`,
`trader.tui`, `trader.commands.stats`,
`trader.cockpit.app`, `trader.ui.palette`, `trader.queue.ledger`,
`trader.state_db.connection`, `trader.semantic.catalog`,
`trader.scheduling.scheduler`, `trader.learnings.raw_store`,
`trader.rotation.schedule`, `trader.reporting.decision_reason`). `trader.tools` est fourni par le finder de
compatibilité dans `trader/__init__.py` et ne correspond plus à un dossier
physique ; `trader.config`, `trader.metadata`, `trader.system`,
`trader.read_models`, `trader.commands`, `trader.cockpit`, `trader.ui`,
`trader.queue`, `trader.state_db`, `trader.semantic`, `trader.scheduling`,
`trader.learnings` et `trader.rotation`, comme `trader.agent_protocol` et
`trader.agent_tools`, sont aussi des packages virtuels de compatibilité. Les
imports internes
doivent viser les packages neutres ou canoniques (`domain/`, `execution/broker`,
`execution/contracts`, `execution/protocols`, `market/`, `market/protocols`, `support/`,
`reporting/read_models/`, `agent/protocol/`, `agent/tools/`, `interfaces/cli/`,
`interfaces/cockpit/`, `interfaces/ui/`, `infrastructure/queue/`,
`infrastructure/state_db/`, `domain/semantic/`, `planning/scheduler.py`,
`agent/learnings/`, `market/rotation/`, `domain/decision_reason.py`). Les adaptateurs concrets restent dans
`execution/broker` et `infrastructure/market_sources/data_source` quand la composition runtime les
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
    ├─ queue: project_shared_context_for_symbol()
    │    (cible + pairs/positions/anomalies ; détails via tools)
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

Fin de cycle, après les décisions : `cycle_finalization.finalize_cycle()`
consolide les learnings si le seuil est atteint, lance la collecte macro
best-effort et mémorise les rejets gross pour le cycle suivant. Le comparateur
historique JSON/SQLite reste une commande manuelle et n'appartient plus au cycle.

---

## 3. Le cycle de décision — de bout en bout

### 3.1 Sélection des symboles dus

`trader/runtime/daemon.py::_select_due_symbols` — en mode normal :
`Scheduler.due_symbols()`
sélectionne les symboles dont le `next_wake` est passé. En mode `--once`/`--bootstrap` :
tout l'univers.

L'univers actif est généré par la rotation (D9/D10) à chaque cycle :
`trader/market/rotation/daemon.py` appelle `compose_active_universe(now)` qui compose
`sticky_all ∪ union(hot-lists des marchés ouverts)` et écrit
`config/universe.yaml` si le contenu change (`trader/runtime/daemon.py`).

### 3.2 Chargement des barres & fraîcheur

`trader/application/cycle/market_snapshot.py` construit le snapshot du cycle :
barres 15m / 5j (runtime décisionnel) + barres 1j / 1y (cockpit daily).
Le snapshot reçoit un `FxRateProvider` local : la politique de cycle consomme les
taux, tandis que le runtime résout `fx.yaml` et le fetch FX via
`trader/runtime/data_source_runtime.py`.
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
- `active_plans_summary` — résumé global compact des veilles/plans actifs
  (`symbol`, `id`, `kind`, `intent` sans conditions) pour conscience d'état
  anti-doublon/OCO
- `semantic.requestable_indicator_ids` — indicateurs disponibles via REQUEST_CONTEXT

Faits calculés par le code et injectés (principe AX : pas de prose) :
- `data_age_m` (âge réel en min des barres) — `trader/runtime/daemon.py`
- `session` (état de la séance par place) — `market.session_snapshot()`
- `active_watches` (détail local des veilles/plans actifs par symbole, avec
  conditions) ; le détail global complet des `TradePlan` ouverts passe par
  l'outil `get_active_plans` (`{rows, as_of}`)

### 3.4 Gate de pertinence (D7 étage A)

`relevance_gate.symbol_needs_llm()` (`trader/planning/relevance_gate.py`) — fonctions pures.
Passe au LLM uniquement si : réveil agent (`agent_wake`), trigger, position ouverte,
régime fort (≥ 70 % de la famille), signal cockpit (`sig`/`stretched`),
ou revue périodique garantie (4 h). Sinon → `quiet_gate` (HOLD sans appel).
`trader/runtime/daemon.py`

### 3.5 Plans armés — exécution sans LLM (D7 étage B)

Les `indicator_watch` à `on_trigger: EXECUTE_ORDER` acceptent côté agent un
`order` Pine-like (`direction`, `qty`, `confidence`, `exit`). À l'armement,
`domain/planning/indicator_watch.py` le compile en ordre interne
(`intent`, `qty`, `confidence`, `exit_plan`). Au déclenchement,
`trader/application/exit/armed_plans.py` résout le cas d'usage et le daemon émet les
logs/événements retournés :
1. `resolve_exit_plan()` — résolution late-binding du stop/TP sur vol fraîche (D11)
2. `armed_order_price_coherent()` — vérif que le prix n'a pas déjà franchi le stop
3. Si conflit multi-scénarios même symbole → réveil planificateur, pas d'exécution
4. Si stale ou position déjà ouverte → annulation + réveil planificateur

Le scénario validé crée une `Decision` directement, sans appel LLM.

### 3.6 Appel LLM batch — `planner_batch.batch_decide()`

Un seul appel `codex_client.decide_batch()` pour tous les symboles dus & frais.
`daemon._batch_decide()` est un wrapper de compatibilité vers
`trader/application/decide/planner_batch.py`. Le contexte partagé est envoyé une fois
(économie D7).

Round-trip `REQUEST_CONTEXT` optionnel : si le LLM demande des indicateurs
supplémentaires (`ContextResearchRequest`), `resolve_indicator_requests()` les calcule
à partir des barres déjà en mémoire et lance un 2e batch sans ré-appeler Codex une
3e fois. Budget : `max_model_calls_per_cycle` (défaut 25) en mode batch legacy
uniquement ; le mode queue/free-iteration n'a pas de cap d'appels et expose seulement
`model_calls_used` comme métrique. `trader/runtime/daemon.py`

Transport : `AcpxBackend.complete()` (`trader/agent/llm.py`) → `acpx --format quiet --allowed-tools "" --no-terminal exec [prompt]`.
Sessions jetables (isolation/idempotence). Fallback : `OpenAICompatibleBackend` (Ollama) si `TRADER_OLLAMA_API_KEY` défini.

### 3.7 Validation & gates pré-exécution

Pour chaque décision (`trader/runtime/daemon.py`) :

| Vérification | Code rejet |
|---|---|
| Intent valide vs action | `invalid_intent` |
| Exit_plan parsable | `invalid_exit_plan:*` |
| `hard_stop` du bon côté | `invalid_exit_plan:hard_stop_wrong_side` |
| RiskGate.check() | `risk:order_value_exceeded`, `risk:gross_exposure_exceeded`, etc. |

`max_risk_per_trade_pct` et les bornes relatives du `hard_stop` sont des
repères informatifs : le daemon trace des warnings, mais ne bloque pas l'ordre
pour les faire respecter. `require_hard_stop` et `confidence_gate_enabled`
peuvent encore être réactivés par configuration, mais le profil exploration les
désactive.

`resolve_exit_plan()` est appliqué aux entrées directes `OPEN_LONG`/`OPEN_SHORT`
(unification avec les armés, D11).

Les helpers purs de cette admission vivent dans
`trader/application/execute/order_admission.py` : intent, hard-stop, clamp de sortie,
quantité ouverte d'un reverse, et métriques de risque d'entrée. L'admission
risque des ouvertures vit dans `trader/application/execute/risk_admission.py` : sizing
`risk_pct`, métriques `risk_unbounded_no_stop`/`risk_pct`, plafond
`max_risk_per_trade_pct`, gate de confiance et `RiskGate.check()` final via des
`Protocol` locaux. Le daemon conserve l'ordre exact des side effects :
scheduling, recorder, broker, trade plans et performance model.

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

`intent: CLOSE | REDUCE | FLIP` → tag `exit_reason = "llm_exit"` (`trader/runtime/daemon.py`).
Raison : le LLM voit un changement de thèse avant le hard_stop.
`plan_store.close_symbol()` à la clôture.

### 4.3 Sortie via plan armé / resolve (D11)

Les triggers `EXECUTE_ORDER` peuvent inclure des ordres de sortie. Chemin identique
au 4.1 une fois le `TradePlan` créé. La résolution late-binding (D11) s'applique
ici avant tout.

---

## 5. Stops & résolution — `resolve_exit_plan`

`trade_plan.resolve_exit_plan()` (`trader/domain/planning/trade_plan.py`) — résout une **intention
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

`trader/market/rotation/venues.py` orchestre une hot-list par place de marché
(TW / EU / US). Le classement intra-venue est recalculé à la clôture de chaque
session. Les politiques pures de sélection et de pool candidat vivent désormais
dans `trader/domain/universe/selection.py` et
`trader/domain/universe/candidate_scope.py` ; la revalidation de la sortie agent
vit dans `trader/application/universe/activation.py`, et les transitions de
scope dans `trader/application/universe/scope_rotation.py`. La persistance de
`venue_state.json`, `universe.yaml` et du cache radar est isolée sous
`trader/infrastructure/files/`.

Univers actif composé à chaque cycle :
`sticky_all ∪ union(hot-lists des marchés OUVERTS à l'instant t)`
*(La `sleeve_24/5` figurait au design D10 mais n'est pas implémentée — forex/commodity retirés, pool 100 % actions.)*

**Sticky** (`sticky_all`) : positions broker + plans armés + exit-watches + ordres
pending — toujours dans l'univers, hors quota, hors logique d'ouverture.

**Hystérésis** : marge Δ (swap seulement si écart d'attractivité ≥ Δ) + dwell K jours
(l'incumbent est protégé K jours). Sortie d'urgence court-circuite K.

Écriture atomique de `universe.yaml` (idempotente — n'écrit que si le contenu
change, jamais vide → fallback dernier univers valide). `market/rotation/venues.py`

Override LLM : l'agent peut ajouter/retirer des symboles avec raison loggée
(`market/rotation/override.py`). Tracé dans `market/rotation/ledger.py`.

---

## 7. Veille / réveils — indicator_watch

> Source canonique des invariants runtime : [`reference/wake-scheduler.md`](reference/wake-scheduler.md).

`trader/domain/planning/indicator_watch.py` — cube `symbol × indicator × timeframe × op × value`.

Trois modes de déclenchement :

| `on_trigger` | Effet | Contexte |
|---|---|---|
| `WAKE` | Réveille le cycle pour ce symbole | Veille simple |
| `WAKE_WITH_ORDER_INTENT` | Réveille + signale une intention d'ordre | Pré-validation |
| `EXECUTE_ORDER` | Exécute l'ordre SANS re-appel LLM | Plan armé D7B |

Un symbole avec `indicator_watch` active ne repasse pas dans le cycle périodique
normal : son `next_wake` est calé sur l'expiration de la veille. Le daemon scanne
les veilles au poll sans appel LLM ; au déclenchement ou à l'expiration, la veille
est retirée et le symbole est réveillé immédiatement (`next_wake = now`) pour que
le cycle suivant le prenne dans `due_symbols`. Les conditions réellement
déclenchées arrivent au LLM via `context.indicator_triggers`; les expirations TTL
arrivent via `context.wake_reasons` (`watch_expired` / `armed_plan_expired`).

**INVARIANT ATOMIQUE** : une seule condition rejetée → toute la veille est rejetée
(jamais de watch amputée). `trader/domain/planning/indicator_watch.py`

TTL max des plans armés : 240 min (aligné sur la revue périodique garantie). `trader/domain/planning/indicator_watch.py`

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
| `decisions.jsonl` | `DecisionRecorder` via `infrastructure/files/decision_ledger.py` | attribution, CLI, cockpit | Une ligne par décision (action, intent, qty, confidence, rationale, executed, reason…) |
| `model_performance.jsonl` | `RuntimeStateWriter.append_model_performance()` via wrapper daemon | `trader/reporting/read_models/attribution.py` | Une ligne par fill (entrée + sortie) — base des round-trips |
| `decision_audit.json` | `trader/runtime/cli.py decisions audit` | `trader/reporting/read_models/meta_performance.py`, CLI status, decision bench | Audit ex-post enrichi des décisions — base du contexte méta-performance |
| `broker.json` | `SimBroker` | `trader/runtime/daemon.py` (reload à chaque cycle) | Positions paper + historique fills |
| `trade_plans.json` | `TradePlanStore` | `trader/planning/exit_engine.py`, `trader/runtime/daemon.py` | Plans ouverts (hard_stop_price, TPs, trailing, watermarks…) |
| `events.jsonl` | `RuntimeStateWriter.append_event()` via wrapper daemon | monitoring / debug | Événements runtime (cycle_started, armed_plan_resolved, watch_triggered…) |
| `history.jsonl` | `RuntimeStateWriter.append_cycle_history()` via wrapper daemon | CLI status | Résumé par cycle (equity, n_executed) |
| `daemon_status.json` | `RuntimeStateWriter.write_status()` via wrapper daemon | cockpit TUI, CLI | Phase courante, PID, decisions_done |
| `current_report.json` | `RuntimeStateWriter.write_current_report()` via wrapper daemon | cockpit TUI | Rapport complet du cycle en cours |
| `learnings.jsonl` | `record_decision()` | `trader/agent/learnings/consolidator.py` | Notes runtime de l'agent (bornées) |
| `learnings_consolidated.json` | `trader/agent/learnings/consolidator.py` | `trader/runtime/daemon.py` (contexte LLM) | Patterns consolidés (≤ seuil bruts → consolidation) |
| `learnings.db` | worker `learnings_sync` + `learnings_ingest` manuel | push borné + outil `recall_learnings` | Store SQLite dérivé : embeddings, FLAIR, traces/rewards et Q-values MemRL |
| `archive/*.jsonl.gz` | `trader/infrastructure/files/ledger_rotation.py` (appelé au démarrage daemon) | `read_rows_with_archive` (analyses) | Mois passés de decisions/events — rotation mensuelle crash-safe |
| `archive/learnings-*.jsonl` | `RawLearningsStore`/`consolidator` | ingestion recall | Évincés + historique des consolidés — plus rien ne se jette |
| `news_items/YYYY-MM-DD.jsonl` | `infrastructure/market_sources/news_feed` | scout + analyste-news | Items de news persistés (dédup uuid, purge 60 j), couverture symbole partielle |
| `macro_calendar.json` + `macro_series/` | `macro_calendar`/`macro_series` | attribution + analyste-news | Dates fusionnées avec fallback versionné + séries macro quotidiennes (DBnomics), potentiellement absentes/stales |
| `news_briefs/YYYY-MM-DD.jsonl` | `runtime/news_macro_runtime` | agent univers + index de situation | Briefs append-only par venue, sourcés et liés au `candidate_scope_id` exact |
| `news_challenger_runs/YYYY-MM-DD.jsonl` | `runtime/news_challenger_runtime` | audit/replay | Couverture partielle, rejets agrégés, challengers et `candidate_run_id` |
| `candidate_scopes/YYYY-MM-DD.jsonl` | rotation close + pré-open | analyste + agent univers | Parent quantitatif close puis enfant final pré-open immuables, avec filiation, pool, baseline, sticky et identifiants de run |
| `global_family_boards/YYYY-MM-DD.jsonl` | `runtime/universe_intelligence_runtime` | trois agents univers + cockpit | Comparaison dérivée des familles TW/EU/US, appendue sur changement matériel ; contexte uniquement, aucune autorité d'allocation |
| `universe_runs/YYYY-MM-DD.jsonl` | `runtime/universe_intelligence_runtime` | audit + activation | Attente/erreur/succès, brief, couverture, sélection et `agent_run_id` |
| `universe_prepared/<scope-hash>.json` | `UniverseRunStore` | activation pré-open | Projection atomique reconstructible pour un `candidate_scope_id` exact |
| `situation_memory.db` | ingestion des briefs | aucun consommateur runtime actuellement | Index FTS5 dérivé ; retrieval de situation non activé |

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
parseurs vivent désormais dans `trader/agent/protocol/` :

- `types.py` : `Decision`, `IndicatorRequest`, `ContextResearchRequest`,
  `BatchToolCallRequest` ;
- `prompts.py` : contrats de sortie, vocabulaire watches/tools, builders ;
- `parsing.py` : extraction JSON, parsing décision/batch/tool calls.

La réponse texte du LLM est traduite en `Decision` (dataclass frozen).
Fail-safe : toute erreur (timeout, JSON invalide, clé manquante) → `Decision.hold(sym, reason)`.
Le LLM ne trade jamais sur une réponse douteuse.

Le contrat LLM visible est `calls` Pine-like. La projection interne `Decision`
inclut ensuite : `action`, `quantity`, `confidence`, `rationale`, `intent`,
`exit_plan`, `exit_update`, `indicator_watch`, `cancel_watch_ids`,
`next_wake_in_minutes`, `learning`.

---

## 10. Outils domaine — la tournée d'outils du LLM (2026-07-02)

Référence canonique : `docs/reference/agent-tools.md`.
Flag : `CASYS_AGENT_TOOLS_ENABLED=1` (actif ; visible au startup dans le log
`[config] … agent_tools=True`).

Le package `trader/agent/tools/` contient le core d'exécution bornée, les
handlers par domaine et `registry.py`. `trader.agent.tools.__init__` réexporte
seulement l'API publique d'exécution ; les validateurs/handlers privés restent
dans leurs modules propriétaires.

Le LLM reçoit UN prompt et peut répondre soit le contrat final, soit
`{"tool_calls": [...]}`. En batch legacy, une seule tournée read-only est offerte
avant décision finale. En queue grain-1, `application.tool_round.resolve_symbol_decision`
peut enchaîner des rounds dans une session acpx persistante, puis force une
décision finale au backstop. Les outils ne passent PAS par acpx
(`--allowed-tools` reste `""`) : le daemon/worker parse, valide contre
`agent.tools.TOOL_REGISTRY` (allowlist Python) et exécute — lecture seule.

Registre read-only (7) : `get_freshness`, `get_indicator_context`
(voie moderne de REQUEST_CONTEXT), `describe_data`, `find_indicators`
(découverte du cube sémantique TraderNexus), `get_active_plans`,
`get_attribution`, `recall_learnings` (mémoire, §11).

Garde-fous : budgets 24 appels/round et 3/symbole en batch legacy
(24/8 en queue grain-1), cap 32 calls sérialisés (sentinel `truncated`), args
scrubbed (profondeur/longueur), contexte borné au chunk ou au symbole, budget
modèle réservé en batch. Tout est tracé dans `runtime.tool_calls` du ledger →
dérivable par `tool_trace.summarize_tools` et `tool_usage` (section
`domain_usage`).

Outils d'ACTION : le contrat public par symbole expose une grammaire Pine-like
JSON (`strategy_entry`, `strategy_exit`, `strategy_close`) plus
`set_next_wake`, `propose_indicator_watch`, `cancel_watch`, `record_learning`.
`strategy_language.py` compile ces appels en primitives de stratégie ; le parser
les projette ensuite vers `Decision`, `exit_plan`, `exit_update`, veilles et
learnings. Les anciens action tools ne sont plus acceptés par le runtime ; les
anciens états sont traités par migration avant lecture durable.

`strategy_exit` a un feedback pré-exécution en queue : le worker lance
`validate_exit_update` en dry-run via `ToolRoundServices.action_validator`. Si le
patch de sortie est rejeté par ce dry-run, il réinjecte `tool_results` avec
`ok:false` dans la même session (2 corrections max). L'application persistante reste
`apply_exit_update_to_open_plan` dans `application/execute/cycle_decision.py` après la
décision finale ; un plan inexistant donne désormais un outcome `rejected`.

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

Chaîne : les learnings ne se jettent plus (archives §8) → le worker best-effort
du daemon (ou `learnings_ingest` manuellement) maintient `state/learnings.db`
(SQLite dérivé, reconstructible : notes +
facettes + FTS5 + embeddings OpenAI pré-calculés) → scoring FLAIR normalisé
par symbole (lift vs base rate + shrinkage bayésien ; verdicts issus du
forward via `decision_quality`) → l'outil
`recall_learnings{symbol?|family?|query?}` approfondit à la demande via un
hybride facettes → FTS5+cosine → RRF → FLAIR+decay+MemRL
(~2-8 ms local, +~3 s max si embed de query OpenAI, dégradation FTS sinon).

La table `recalls` trace quelles notes ont servi quelle décision. Après maturité,
le worker applique `Q ← Q + 0.1 × (reward − Q)` ; `q_updates` shrinke son poids
dans le ranking. En queue `decision_focus_v1`, les anciens slots permanents
`by_symbol` restent supprimés : `recall_learnings` est un retrieval épisodique
borné, complété à la demande par l'outil.

## 12. Gestion des données — rotation et archives (2026-07-02)

Doc : `docs/superpowers/specs/2026-07-02-agent-data-lifecycle.md`.

- **Rotation mensuelle** (`infrastructure/files/ledger_rotation`, au démarrage du daemon) :
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

## 13. Pipeline macro/news et sélection d'univers (livré 2026-07-10)

Spec : `docs/superpowers/specs/2026-07-02-macro-analyste-news-spec.md` ;
sources : `docs/superpowers/specs/2026-07-02-macro-data-sources.md`.
Items de news persistés + calendrier FOMC/CPI (`macro_next` par décision) +
séries macro quotidiennes via DBnomics (zéro clé). À chaque clôture de venue, la
rotation persiste un parent quantitatif top 40. À T-90 du pré-open, elle fusionne
les challengers, notamment overnight, et persiste un enfant final lié au parent.
L'analyste async produit le brief exact de cet enfant ; l'agent univers async en
consomme une projection bornée ainsi qu'un `GlobalFamilyBoard` commun comparatif,
sans quotas ni capital, et prépare sa propre hotlist. À T-15, le runtime
active seulement la préparation du même scope, sinon la baseline avec une raison
de fallback explicite. Les sticky sont ajoutés ensuite hors quota.

Les deux runners sont activés par défaut et coupés séparément avec
`CASYS_NEWS_MACRO_ANALYST_ENABLED=0` et
`CASYS_UNIVERSE_INTELLIGENCE_ENABLED=0`. Ils sont fail-open et ne bloquent jamais
le daemon. Un état live ancien ne crée pas ces artefacts rétroactivement : il faut
un daemon actif, la prochaine clôture puis le prochain pré-open de chaque venue.

La couverture reste déclarée partielle : news symboles limitées au corpus local,
aucune source globale indépendante garantie, calendrier local/fallback et séries
potentiellement stales. `state/last_regime.json` est désormais écrit atomiquement
avec timestamp et couverture `active_tradable_universe`; il est ignoré après
96 h. `situation_memory.db` indexe les briefs, mais aucun retrieval historique
n'est activé ; il ne constitue jamais une source de vérité du présent.

---

*Doutes / points à valider humainement listés dans le résumé de livraison.*
