# Documentation — casys-trader

Point d'entrée de la doc. Deux choses ici : **où vit chaque type de doc**
(cadre Diátaxis) et **la carte de couverture** (quel sous-système est
documenté, où, et quels trous restent).

> Statut carte : **v3 — 2026-07-06**, alignée sur le refacto capability-based
> (packages `trader/<capacité>/`). Les `🟡`/`❌` sont un backlog priorisé ;
> corriger une ligne = ouvrir la doc citée.

---

## Où vit quoi (Diátaxis)

| Quadrant | Dossier | Contenu | Question |
|---|---|---|---|
| **Reference** | [`docs/reference/`](reference/README.md) | Ce que fait chaque sous-système **aujourd'hui** : comportement, invariants, garde-fous, codes | « comment ça marche *maintenant* ? » |
| **Explanation** | `docs/architecture.md` | Le cycle de bout en bout, le pourquoi | « pourquoi comme ça ? » |
| **How-to** | [`docs/how-to/`](how-to/README.md) | Runbooks : déployer, relancer, mesurer, lire les logs | « comment je fais X ? » |
| **Décisions (ADR)** | [`docs/decisions/`](decisions/README.md) | Journal D1-D14, datées, immuables | « quelle décision, pourquoi ? » |
| **Postmortems** | [`docs/postmortems/`](postmortems/README.md) | Incidents + fix | « qu'est-ce qui a cassé ? » |
| **Specs / plans** | [`docs/superpowers/`](superpowers/README.md) | Intention de conception au moment T | « comment on l'a conçu ? » |

### Règles de consolidation

- `docs/reference/` porte la vérité runtime **canonique** : comportement actuel,
  invariants, champs d'état, codes et limites.
- `docs/architecture.md` explique le cycle de bout en bout et renvoie aux pages
  `reference/` pour les détails ; il ne doit plus devenir le seul endroit où vit
  un sous-système.
- `docs/how-to/` contient les procédures opérateur ; il lie vers `reference/`
  au lieu de recopier les invariants.
- `docs/decisions/` est immuable : pourquoi on a tranché, pas une page runtime
  à corriger à chaque refacto.
- `docs/superpowers/specs/` et `docs/superpowers/plans/` sont historiques :
  après livraison, consolider le comportement dans `reference/` et garder le
  plan comme trace de chantier.

`architecture.md` (§1-13) couvre l'explication du **cœur runtime**. Les sujets
transverses ou fréquemment diagnostiqués ont une page `reference/` dédiée.

Index de dossiers : [`reference`](reference/README.md), [`how-to`](how-to/README.md),
[`decisions`](decisions/README.md), [`postmortems`](postmortems/README.md),
[`superpowers`](superpowers/README.md).

### Documents racine

| Document | Rôle | Source de vérité |
|---|---|---|
| [`architecture.md`](architecture.md) | Explication bout-en-bout du cycle runtime | Code + pages `reference/` |
| [`decisions-business.md`](decisions-business.md) | Synthèse lisible des décisions métier | [`decisions/registre-decisions-metier.md`](decisions/registre-decisions-metier.md) |
| [`etat-systeme.md`](etat-systeme.md) | État vivant / snapshot opérateur | À vérifier contre `state/` et le process live avant décision |
| [`README.md`](README.md) | Index Diátaxis + carte de couverture | Cette page |

---

## Carte de couverture (par capacité / package)

Légende : ✅ couvert · 🟡 partiel / dispersé / potentiellement périmé · ❌ trou.

### Cœur runtime & décision
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Orchestration du cycle | `runtime/daemon` | ✅ | archi §2-3 | D7 |
| Sélection des dus / veilles | `planning/scheduler`, `planning/indicator_watch` | ✅ | **`reference/wake-scheduler.md`**, archi §3.1, §7 | D7, D9, D10 |
| Batch LLM / planificateur | `application/planner_batch` | ✅ | archi §3.6 | D7 |
| Contexte marché (snapshot) | `application/market_snapshot` | ✅ | archi §3.2-3.3 | — |
| Enregistrement décision | `application/decision_recorder` | ✅ | archi §3.8, §8 | — |
| Finalisation fin de cycle | `runtime/cycle_finalization` | ✅ | archi §1.1, §2 ; `reference/task-queue.md` | queue paper activée |
| Gate de pertinence (coût) | `planning/relevance_gate` | ✅ | archi §3.4 | D7A |
| **File de tâches durable** | `runtime/queue_runtime`, `infrastructure/queue/*` (ledger, pools, worker), `application/{queue_dispatch,execute_queue_dispatch,execute_queue_plan}` | ✅ | **`reference/task-queue.md`** | queue paper activée |

### Actions & exécution
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| **Risk gate** | `execution/risk` | ✅ | **`reference/risk-gate.md`** | — |
| Plans armés (EXECUTE_ORDER) | `planning/indicator_watch`, `planning/trade_plan`, `runtime/daemon` | ✅ | **`reference/wake-scheduler.md`**, archi §3.5, §4.3 ; registre | D7B, D11, D12 |
| Admission d'ordre | `application/order_admission`, `application/risk_admission` | ✅ | **`reference/execution.md`** | — |
| Accounting post-fill | `application/fill_outcome`, `runtime/daemon` writer | ✅ | archi §1.1, §8 | — |
| Effets plans post-fill | `application/fill_plan_effects`, `planning/trade_plan` | ✅ | archi §1.1, §4 | — |
| Allocateur budget gross | `market/gross_priority` | ✅ | **`reference/execution.md`** | — |
| Exécution / broker | `execution/broker`, `execution/portfolio` | ✅ | **`reference/execution.md`** | — |
| Sorties automatiques | `planning/exit_engine` | ✅ | archi §4 | — |
| Stops & résolution au tir | `planning/exit_engine` (`resolve_exit_plan`) | ✅ | archi §5 | D11 |

### Données
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Sources marché & fraîcheur | `runtime/data_source_runtime`, `market/data_source`, `market/market_data`, `market/ib_source` | ✅ | archi §1.1, §3.2 | — |
| **Conversion FX** | `market/fx`, `market/fx_rates` | ✅ | **`reference/fx.md`** | chantier FX |
| Fil d'actu (news) | `market/news_feed` | ✅ | **`reference/news.md`** | — |
| Macro | `market/macro_calendar`, `market/macro_series` | ✅ | **`reference/macro.md`** | — |
| Cycle de vie / rotation | `runtime/daemon_bootstrap`, `runtime/cycle_dispatch`, `runtime/cycle_reporting`, `runtime/runtime_shutdown`, `runtime/ledger_rotation`, `reporting/decision_ledger` | ✅ | archi §1.1, §12 | — |
| État persistant | `runtime/daemon_bootstrap`, `state/*.jsonl`, `trade_plans.json`, `scheduler.json`, `infrastructure/state_db/*` | ✅ | archi §1.1, §8, `reference/task-queue.md` | — |

### Univers & régime
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Radar / rotation / hot-sets | `runtime/market_rotation_runtime`, `market/rotation/*` (core, venues, collectors, schedule, override, wiring) | ✅ | **`reference/universe-rotation.md`**, archi §1.1 | D9, D10, D13 |
| Régime (marché + familial) | `market/regime`, `market/family_regime` | ✅ | **`reference/regime.md`** | D2 |
| Config univers & portefeuille | `config/*.yaml`, `support/config/pool`, `support/config/portfolio` | ✅ | **`reference/config.md`** | D9/D10/D13 |

### LLM & agent
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Transport LLM / acpx | `agent/llm`, `agent/client` | ✅ | archi §9.1-9.2 | — |
| Contrat / protocole (prompts + mandat) | `agent/protocol/` (types, prompts, parsing), `mandate/` | ✅ | **`reference/llm-contract.md`** | — |
| Vocabulaire des raisons de décision | `domain/decision_reason` | ✅ | **`reference/reporting.md`** | — |
| Contexte agent (cockpit) | `agent/context` | ✅ | **`reference/agent-context.md`** | — |
| Outils domaine (read-only) | `agent/tools/` (9 handlers) | ✅ | **`reference/agent-tools.md`** | — |
| Mémoire / recall / learnings (RAG) | `agent/learnings/` (store, consolidator, embeddings) | ✅ | **`reference/learnings-rag.md`** | — |
| Couche sémantique | `domain/semantic/catalog` | ✅ | **`reference/semantic.md`** | — |

### Observabilité
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Cockpit TUI | `interfaces/cockpit/` (app, supervisor, events), `interfaces/ui/`, `reporting/read_models/runtime_state` | ✅ | **`reference/cockpit.md`** | — |
| Attribution / performance | `reporting/` (attribution, meta_performance, decision_audit, decision_bench, decision_ledger, stats, tool_usage) + `reporting/audit/decision_quality` + `reporting/bench/decision_bench` + `reporting/ledger/decision_ledger` + `reporting/read_models/{attribution,live_kpis,meta_performance,tool_usage}` + `reporting/renderers/{live_kpis,tool_usage}` | ✅ | **`reference/reporting.md`** | — |
| Commandes opérateur | `interfaces/cli/` + alias virtuels legacy `trader.commands.*`, `trader.stats`, `trader.attribution`, `trader.tool_usage`, `trader.tui` | ✅ | archi §1.1, **`reference/reporting.md`** | — |
| Logging | `runtime/logging_setup` (`CASYS_LOG_LEVEL`, museler ib_async) | ✅ | archi §10.1 | — |

### How-to / runbooks
| Runbook | Réf | Où |
|---|---|---|
| Déployer / relancer / arrêter le daemon (superviseur, SIGINT hors-batch) | ✅ | `how-to/run-the-daemon.md` |
| Lire les logs (Gonzo `make logs`, `CASYS_LOG_LEVEL=DEBUG`, Dstl8.Lite) | ✅ | `how-to/read-logs.md` |
| Mesurer (`measure_d7.py`) & rejouer des plans (`plan_replay`) | ✅ | `how-to/measure-and-replay.md` |
| Configurer l'apparence cockpit terminal | ✅ | `how-to/cockpit-glass.md` |

---

## Backlog v2 — vidé ✅ (2026-07-03)

Les 5 trous prioritaires identifiés à la v2 sont comblés :

1. ~~Risk gate~~ ✅ [`reference/risk-gate.md`](reference/risk-gate.md)
2. ~~Conversion FX~~ ✅ [`reference/fx.md`](reference/fx.md)
3. ~~Runbooks how-to~~ ✅ [`how-to/`](how-to/README.md) (run-the-daemon, read-logs, measure-and-replay, cockpit-glass)
4. ~~Cockpit & attribution~~ ✅ [`cockpit`](reference/cockpit.md) · [`reporting`](reference/reporting.md)
5. ~~Config univers/portefeuille~~ ✅ [`reference/config.md`](reference/config.md)

**Couverture complète ✅ (2026-07-03, consolidée v3 le 2026-07-06)** — tous les
sous-systèmes de la carte ont désormais une page `reference/` **fact-checkée**
(18 pages reference + index ; 4 how-to + index). Nouveaux sous-systèmes documentés depuis la v2 :
`infrastructure/queue/*` (file de tâches durable branchée au daemon en paper) et
`planning/scheduler` / `planning/indicator_watch` (réveils, watches, plans armés).
Maintenir : quand un module change, mettre à jour sa page (lire code → éditer →
re-fact-check si substantiel).

Méthode : lire le code → écrire la page `reference/` (comportement/invariants/codes)
→ passer la ligne ✅ dans la carte. Un fact-check Codex de chaque réf vs le code
est recommandé avant de s'y fier (réf fausse pire que pas de réf).
