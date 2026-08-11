# Documentation — casys-trader

Point d'entrée de la doc. Deux choses ici : **où vit chaque type de doc**
(cadre Diátaxis) et **la carte de couverture** (quel sous-système est
documenté, où, et quels trous restent).

> Statut carte : **v5 — 2026-08-11**, vérifiée contre le runtime courant :
> SQLite canonique, queue grain-symbole, pilote de preuve, profils LLM,
> learnings automatiques et pipeline de rapports global/macro/régional/micro.
> Les `🟡`/`❌` sont un backlog priorisé ; corriger une ligne = ouvrir la doc citée.

---

## Où vit quoi (Diátaxis)

| Quadrant | Dossier | Contenu | Question |
|---|---|---|---|
| **Tutorials** | [`docs/tutorials/`](tutorials/README.md) | Parcours guidés pour apprendre sur un état paper | « accompagne-moi pour comprendre » |
| **How-to** | [`docs/how-to/`](how-to/README.md) | Runbooks : relancer, mesurer, maintenir, diagnostiquer | « comment je fais X ? » |
| **Reference** | [`docs/reference/`](reference/README.md) | Comportement actuel, invariants, garde-fous et formats | « comment ça marche maintenant ? » |
| **Explanation** | [`architecture.md`](architecture.md), [`decisions-business.md`](decisions-business.md) | Vue d'ensemble, raisons et relations entre concepts | « pourquoi est-ce conçu ainsi ? » |

Documents complémentaires, hors des quatre quadrants :

| Registre | Dossier | Rôle |
|---|---|---|
| **Décisions (ADR)** | [`docs/decisions/`](decisions/README.md) | Journal D1-D15 daté, à ne pas réécrire comme une référence runtime |
| **Postmortems** | [`docs/postmortems/`](postmortems/README.md) | Incidents, causes et corrections |
| **Specs / plans** | [`docs/superpowers/`](superpowers/README.md) | Intention de conception au moment T |

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

`architecture.md` (§1-14) couvre l'explication du **cœur runtime**. Les sujets
transverses ou fréquemment diagnostiqués ont une page `reference/` dédiée.

Index de dossiers : [`reference`](reference/README.md), [`how-to`](how-to/README.md),
[`tutorials`](tutorials/README.md),
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
| Décision LLM grain-symbole / compat batch | `application/decide`, `runtime/decision_dispatch_runtime`, `infrastructure/queue` | ✅ | archi §2, §3.6 ; **`reference/task-queue.md`** | D7 |
| Contexte marché (snapshot) | `application/cycle/market_snapshot` | ✅ | archi §3.2-3.3 | — |
| Enregistrement décision | `application/record/decision_recorder` | ✅ | archi §3.8, §8 | — |
| Finalisation fin de cycle | `runtime/cycle_finalization` | ✅ | archi §1.1, §2 ; `reference/task-queue.md` | queue paper activée |
| Gate de pertinence (coût) | `planning/relevance_gate` | ✅ | archi §3.4 | D7A |
| **File de tâches durable** | `runtime/queue_runtime`, `infrastructure/queue/*` (ledger, pools, worker), `application/{queue_dispatch,execute_queue_dispatch,execute_queue_plan}` | ✅ | **`reference/task-queue.md`** | queue paper activée |
| **Pilote de preuve du processus** | `domain/process_trace`, `runtime/process_*`, `state_db/process_event_store` | ✅ | **`reference/process-governance.md`**, archi §14 | observationnel |

### Actions & exécution
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| **Risk gate** | `execution/risk` | ✅ | **`reference/risk-gate.md`** | — |
| Plans armés (EXECUTE_ORDER) | `planning/indicator_watch`, `planning/trade_plan`, `runtime/daemon` | ✅ | **`reference/wake-scheduler.md`**, archi §3.5, §4.3 ; registre | D7B, D11, D12 |
| Admission d'ordre | `application/execute/order_admission`, `application/execute/risk_admission` | ✅ | **`reference/execution.md`** | — |
| Accounting post-fill | `application/execute/fill_outcome`, `runtime/daemon` writer | ✅ | archi §1.1, §8 | — |
| Effets plans post-fill | `application/exit/fill_plan_effects`, `planning/trade_plan` | ✅ | archi §1.1, §4 | — |
| Allocateur budget gross | `domain/market/gross_priority` | ✅ | **`reference/execution.md`** | — |
| Exécution / broker | `execution/broker`, `execution/portfolio` | ✅ | **`reference/execution.md`** | — |
| Sorties automatiques | `planning/exit_engine` | ✅ | archi §4 | — |
| Stops & résolution au tir | `planning/exit_engine` (`resolve_exit_plan`) | ✅ | archi §5 | D11 |

### Données
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Sources marché & fraîcheur | `runtime/data_source_runtime`, `infrastructure/market_sources/data_source`, `market/market_data`, `infrastructure/market_sources/ib_source` | ✅ | archi §1.1, §3.2 | — |
| **Conversion FX** | `domain/market/fx`, `infrastructure/market_sources/fx_rates` | ✅ | **`reference/fx.md`** | chantier FX |
| News, challengers et analyste | `infrastructure/market_sources/news_feed`, `runtime/news_challenger_runtime`, `runtime/news_macro_runtime`, `agent/news_macro` | ✅ | **`reference/news.md`** | D15 |
| Macro | `market/macro_calendar`, `infrastructure/market_sources/macro_series`, `runtime/news_macro_runtime` | ✅ | **`reference/macro.md`** | D15 |
| Cycle de vie / rotation | `runtime/daemon_bootstrap`, `runtime/cycle_dispatch`, `runtime/cycle_reporting`, `runtime/runtime_shutdown`, `infrastructure/files/ledger_rotation`, `infrastructure/files/decision_ledger` | ✅ | archi §1.1, §12 | — |
| Intelligence entreprise | `runtime/company_intelligence_runtime`, `infrastructure/market_sources/company`, `state_db/company_*` | ✅ | **`reference/company-intelligence.md`** | advisory |
| État persistant | `runtime/daemon_bootstrap`, `state/casys.db`, files/ledgers spécialisés, `infrastructure/state_db/*` | ✅ | archi §1.1, §8, `reference/task-queue.md` | SQLite paper canonique |

### Univers & régime
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Radar / rotation / challengers / hotlist | `runtime/market_rotation_runtime`, `runtime/news_challenger_runtime`, `market/rotation/*`, `domain/universe` | ✅ | **`reference/universe-rotation.md`**, archi §1.1 | D9, D10, D13, D15 |
| Pipeline global/régional → Trader | `runtime/universe_intelligence_runtime`, `domain/universe/mandate`, `agent/universe` | ✅ | **`reference/universe-trader-pipeline.md`**, archi §13 | advisory + activation exacte |
| Régime (marché + familial) | `domain/market/regime`, `domain/market/family_regime` | ✅ | **`reference/regime.md`** | D2 |
| Config univers & portefeuille | `config/*.yaml`, `support/config/pool`, `support/config/portfolio` | ✅ | **`reference/config.md`** | D9/D10/D13/D15 |

### LLM & agent
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Transport LLM / acpx | `agent/llm`, `infrastructure/llm/acpx_backend`, `agent/client` | ✅ | archi §9.1-9.2 ; **`reference/codex-home-isole.md`** | — |
| Presets des cinq rôles | `scripts/model_preset.py`, `ops/model-presets`, `ops/codex-home` | ✅ | **`reference/model-presets.md`** | brain Luna medium, analystes Sol low |
| Contrat / protocole (prompts + mandat) | `agent/protocol/` (types, prompts, parsing), `mandate/` | ✅ | **`reference/llm-contract.md`** | — |
| Vocabulaire des raisons de décision | `domain/decision_reason` | ✅ | **`reference/reporting.md`** | — |
| Contexte agent (cockpit) | `agent/context` | ✅ | **`reference/agent-context.md`** | — |
| Outils domaine et langage d'action | `agent/tools/` (7 outils read-only) + compilateur d'action | ✅ | **`reference/agent-tools.md`** | — |
| Mémoire / recall / learnings (RAG) | `agent/learnings/` (store, consolidator, embeddings) | ✅ | **`reference/learnings-rag.md`** | — |
| Mémoire de situation (index dérivé, retrieval non câblé) | `infrastructure/state_db/situation_memory_store` | 🟡 | **`reference/news.md`**, **`reference/agent-knowledge-architecture.md`** | D15 |
| Couche sémantique | `domain/semantic/catalog` | ✅ | **`reference/semantic.md`** | — |

### Observabilité
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Cockpit TUI + galerie Reports | `interfaces/cockpit/` (app, supervisor, events, reports), `interfaces/ui/`, `reporting/read_models/runtime_state` | ✅ | **`reference/cockpit.md`** | lecture seule des rapports |
| Attribution / performance | `reporting/` (attribution, meta_performance, decision_audit, decision_bench, decision_ledger, stats, tool_usage) + `reporting/audit/decision_quality` + `reporting/bench/decision_bench` + `reporting/ledger/decision_ledger` + `reporting/read_models/{attribution,live_kpis,meta_performance,tool_usage}` + `reporting/renderers/{live_kpis,tool_usage}` | ✅ | **`reference/reporting.md`** | — |
| Commandes opérateur | `interfaces/cli/` + alias virtuels legacy `trader.commands.*`, `trader.stats`, `trader.attribution`, `trader.tool_usage`, `trader.tui` | ✅ | archi §1.1, **`reference/reporting.md`** | — |
| Logging | `runtime/logging_setup` (`CASYS_LOG_LEVEL`, museler ib_async) | ✅ | archi §10.1 | — |

### How-to / runbooks
| Runbook | Réf | Où |
|---|---|---|
| Déployer / relancer / arrêter le daemon (superviseur, SIGINT hors-batch) | ✅ | `how-to/run-the-daemon.md` |
| Lire les logs (Gonzo `make logs`, `CASYS_LOG_LEVEL=DEBUG`, Dstl8.Lite) | ✅ | `how-to/read-logs.md` |
| Changer/vérifier les modèles et le profil isolé | ✅ | `how-to/manage-model-presets.md` |
| Maintenir les learnings, embeddings, FLAIR/MemRL | ✅ | `how-to/maintain-learnings.md` |
| Rafraîchir et diagnostiquer les quatre niveaux de rapports | ✅ | `how-to/refresh-and-diagnose-reports.md` |
| Mesurer (`measure_d7.py`) & rejouer des plans (`plan_replay`) | ✅ | `how-to/measure-and-replay.md` |
| Configurer l'apparence cockpit terminal | ✅ | `how-to/cockpit-glass.md` |

---

## Maintenance de la couverture

Les 5 trous prioritaires identifiés à la v2 sont comblés :

1. ~~Risk gate~~ ✅ [`reference/risk-gate.md`](reference/risk-gate.md)
2. ~~Conversion FX~~ ✅ [`reference/fx.md`](reference/fx.md)
3. ~~Runbooks how-to~~ ✅ [`how-to/`](how-to/README.md) (run-the-daemon, read-logs, measure-and-replay, cockpit-glass)
4. ~~Cockpit & attribution~~ ✅ [`cockpit`](reference/cockpit.md) · [`reporting`](reference/reporting.md)
5. ~~Config univers/portefeuille~~ ✅ [`reference/config.md`](reference/config.md)

La consolidation v5 compte **23 pages Reference + index**, **7 How-to + index**
et **1 tutoriel + index**. Elle ajoute notamment la gouvernance/provenance du cycle, les presets
des cinq rôles, company intelligence, le pipeline Univers → Trader, la galerie
Reports et les procédures de maintenance correspondantes. Elle ne transforme
pas les specs/plans historiques en vérité courante et ne prétend pas que les
snapshots de `etat-systeme.md` restent vrais sans vérification live.

Maintenir : quand un module change, mettre à jour sa page (lire code → éditer →
re-fact-check si substantiel). Les seules lignes volontairement `🟡` sont les
capacités non encore branchées de bout en bout, comme le retrieval de mémoire de
situation.

Méthode : lire le code → écrire la page `reference/` (comportement/invariants/codes)
→ passer la ligne ✅ dans la carte. Un fact-check Codex de chaque réf vs le code
est recommandé avant de s'y fier (réf fausse pire que pas de réf).
