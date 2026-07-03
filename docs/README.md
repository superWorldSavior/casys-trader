# Documentation — casys-trader

Point d'entrée de la doc. Deux choses ici : **où vit chaque type de doc**
(cadre Diátaxis) et **la carte de couverture** (quel sous-système est
documenté, où, et quels trous restent).

> Statut carte : **v2 — 2026-07-03**, alignée sur le refacto capability-based
> (packages `trader/<capacité>/`). Les `🟡`/`❌` sont un backlog priorisé ;
> corriger une ligne = ouvrir la doc citée.

---

## Où vit quoi (Diátaxis)

| Quadrant | Dossier | Contenu | Question |
|---|---|---|---|
| **Reference** | `docs/reference/` | Ce que fait chaque sous-système **aujourd'hui** : comportement, invariants, garde-fous, codes | « comment ça marche *maintenant* ? » |
| **Explanation** | `docs/architecture.md` | Le cycle de bout en bout, le pourquoi | « pourquoi comme ça ? » |
| **How-to** | `docs/how-to/` | Runbooks : déployer, relancer, mesurer, lire les logs | « comment je fais X ? » |
| **Décisions (ADR)** | `docs/decisions/registre-decisions-metier.md` | Journal D1-D12, datées, immuables | « quelle décision, pourquoi ? » |
| **Postmortems** | `docs/postmortems/` | Incidents + fix | « qu'est-ce qui a cassé ? » |
| **Specs / plans** | `docs/specs/`, `docs/superpowers/` | Intention de conception au moment T | « comment on l'a conçu ? » |
| **Recherche** | `docs/research/` | Explorations | « qu'a-t-on exploré ? » |

`architecture.md` (§1-13) couvre bien la Reference+Explanation du **cœur runtime**.
Les **trous** ci-dessous obtiennent une page `reference/` dédiée ; les runbooks
`how-to/` (quadrant entier absent) sont à créer.

Pages `reference/` écrites : [`risk-gate`](reference/risk-gate.md), [`fx`](reference/fx.md), [`cockpit`](reference/cockpit.md), [`reporting`](reference/reporting.md), [`config`](reference/config.md), [`learnings-rag`](reference/learnings-rag.md), [`agent-tools`](reference/agent-tools.md), [`macro`](reference/macro.md), [`llm-contract`](reference/llm-contract.md), [`universe-rotation`](reference/universe-rotation.md), [`execution`](reference/execution.md), [`news`](reference/news.md), [`regime`](reference/regime.md), [`agent-context`](reference/agent-context.md), [`semantic`](reference/semantic.md), [`task-queue`](reference/task-queue.md).
Pages `how-to/` écrites : [`run-the-daemon`](how-to/run-the-daemon.md), [`read-logs`](how-to/read-logs.md), [`measure-and-replay`](how-to/measure-and-replay.md).

---

## Carte de couverture (par capacité / package)

Légende : ✅ couvert · 🟡 partiel / dispersé / potentiellement périmé · ❌ trou.

### Cœur runtime & décision
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Orchestration du cycle | `runtime/daemon` | ✅ | archi §2-3 | D7 |
| Sélection des dus / veilles | `tools/scheduler`, `planning/indicator_watch` | ✅ | archi §3.1, §7 | D7, D9, D10 |
| Batch LLM / planificateur | `application/planner_batch` | ✅ | archi §3.6 | D7 |
| Contexte marché (snapshot) | `application/market_snapshot` | ✅ | archi §3.2-3.3 | — |
| Enregistrement décision | `application/decision_recorder` | ✅ | archi §3.8, §8 | — |
| Gate de pertinence (coût) | `planning/relevance_gate` | ✅ | archi §3.4 | D7A |
| **File de tâches durable** | `queue/*` (ledger, pools, worker) | 🟡 | **`reference/task-queue.md`** | — Phase 0, non branché |

### Actions & exécution
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| **Risk gate** | `execution/risk` | ✅ | **`reference/risk-gate.md`** | — |
| Plans armés (EXECUTE_ORDER) | `planning/indicator_watch`, `planning/trade_plan`, `runtime/daemon` | ✅ | archi §3.5, §4.3 ; registre | D7B, D11, D12 |
| Admission d'ordre | `application/order_admission` | ✅ | **`reference/execution.md`** | — |
| Allocateur budget gross | `market/gross_priority` | ✅ | **`reference/execution.md`** | — |
| Exécution / broker | `tools/execution`, `tools/portfolio` | ✅ | **`reference/execution.md`** | — |
| Sorties automatiques | `planning/exit_engine` | ✅ | archi §4 | — |
| Stops & résolution au tir | `planning/exit_engine` (`resolve_exit_plan`) | ✅ | archi §5 | D11 |

### Données
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Sources marché & fraîcheur | `tools/data_source`, `tools/market`, `tools/ib_source` | ✅ | archi §3.2 | — |
| **Conversion FX** | `market/fx`, `market/fx_rates` | ✅ | **`reference/fx.md`** | chantier FX |
| Fil d'actu (news) | `tools/news_feed` | ✅ | **`reference/news.md`** | — |
| Macro | `market/macro_calendar`, `market/macro_series` | ✅ | **`reference/macro.md`** | — |
| Cycle de vie / rotation | `runtime/ledger_rotation`, `reporting/decision_ledger` | ✅ | archi §12 | — |
| État persistant | `state/*.jsonl`, `trade_plans.json`, `scheduler.json` | ✅ | archi §8 | — |

### Univers & régime
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Radar / rotation / hot-sets | `rotation/*` (core, venues, collectors, schedule, override, wiring) | ✅ | **`reference/universe-rotation.md`** | D9, D10, D13 |
| Régime (marché + familial) | `market/regime`, `market/family_regime` | ✅ | **`reference/regime.md`** | D2 |
| Config univers & portefeuille | `config/*.yaml`, `config/pool`, `config/portfolio` | ✅ | **`reference/config.md`** | D9/D10/D13 |

### LLM & agent
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Transport LLM / acpx | `agent/llm`, `agent/client` | ✅ | archi §9.1-9.2 | — |
| Contrat / protocole (prompts + mandat) | `agent_protocol/` (types, prompts, parsing), `mandate/` | ✅ | **`reference/llm-contract.md`** | — |
| Contexte agent (cockpit) | `agent/context` | ✅ | **`reference/agent-context.md`** | — |
| Outils domaine (read-only) | `agent_tools/` (9 handlers) | ✅ | **`reference/agent-tools.md`** | — |
| Mémoire / recall / learnings (RAG) | `learnings/` (store, consolidator, embeddings) | ✅ | **`reference/learnings-rag.md`** | — |
| Couche sémantique | `semantic/catalog` | ✅ | **`reference/semantic.md`** | — |

### Observabilité
| Sous-système | Package/module | Réf | Où | Décisions |
|---|---|---|---|---|
| Cockpit TUI | `cockpit/` (app, supervisor, events), `ui/`, `read_models/runtime_state` | ✅ | **`reference/cockpit.md`** | — |
| Attribution / performance | `reporting/` (attribution, meta_performance, decision_audit, decision_bench, stats, tool_usage) | ✅ | **`reference/reporting.md`** | — |
| Logging | `runtime/logging_setup` (`CASYS_LOG_LEVEL`, museler ib_async) | ✅ | archi §10.1 | — |

### How-to / runbooks
| Runbook | Réf | Où |
|---|---|---|
| Déployer / relancer / arrêter le daemon (superviseur, SIGINT hors-batch) | ✅ | `how-to/run-the-daemon.md` |
| Lire les logs (Gonzo `make logs`, `CASYS_LOG_LEVEL=DEBUG`, Dstl8.Lite) | ✅ | `how-to/read-logs.md` |
| Mesurer (`measure_d7.py`) & rejouer des plans (`plan_replay`) | ✅ | `how-to/measure-and-replay.md` |

---

## Backlog v2 — vidé ✅ (2026-07-03)

Les 5 trous prioritaires identifiés à la v2 sont comblés :

1. ~~Risk gate~~ ✅ [`reference/risk-gate.md`](reference/risk-gate.md)
2. ~~Conversion FX~~ ✅ [`reference/fx.md`](reference/fx.md)
3. ~~Runbooks how-to~~ ✅ [`how-to/`](how-to/) (run-the-daemon, read-logs, measure-and-replay)
4. ~~Cockpit & attribution~~ ✅ [`cockpit`](reference/cockpit.md) · [`reporting`](reference/reporting.md)
5. ~~Config univers/portefeuille~~ ✅ [`reference/config.md`](reference/config.md)

**Couverture complète ✅ (2026-07-03)** — tous les sous-systèmes de la carte ont
désormais une page `reference/` **fact-checkée** (16 pages reference + 3 how-to).
Nouveau sous-système documenté : `queue/*` (file de tâches durable, Phase 0 —
cœur en place, non branché au daemon). Maintenir : quand un module change, mettre
à jour sa page (lire code → éditer → re-fact-check si substantiel).

Méthode : lire le code → écrire la page `reference/` (comportement/invariants/codes)
→ passer la ligne ✅ dans la carte. Un fact-check Codex de chaque réf vs le code
est recommandé avant de s'y fier (réf fausse pire que pas de réf).
