# Documentation — casys-trader

Point d'entrée de la doc. Deux choses ici : **où vit chaque type de doc**
(cadre Diátaxis) et **la carte de couverture** (quel sous-système est
documenté, où, et quels trous restent).

> Statut carte : **v1 — 2026-07-02**, premier passage. Les `🟡`/`❌` sont un
> backlog, pas un jugement définitif ; corriger une ligne = ouvrir la doc citée.

---

## Où vit quoi (Diátaxis)

| Quadrant | Dossier | Contenu | Question à laquelle il répond |
|---|---|---|---|
| **Reference** | `docs/reference/` *(à créer)* | Ce que fait chaque sous-système **aujourd'hui** : comportement, invariants, garde-fous | « comment ça marche *maintenant* ? » |
| **Explanation** | `docs/architecture.md` | Le cycle de bout en bout, le pourquoi des choix | « pourquoi c'est fait comme ça ? » |
| **How-to** | `docs/how-to/` *(à créer)* | Runbooks : déployer, relancer le daemon, mesurer, lire les logs | « comment je fais X ? » |
| **Décisions (ADR)** | `docs/decisions/registre-decisions-metier.md` | Journal D1-D12 : décisions de fonctionnement, datées, immuables | « pourquoi cette décision, et laquelle ? » |
| **Postmortems** | `docs/postmortems/` | Incidents analysés + fix | « qu'est-ce qui a cassé et pourquoi ? » |
| **Specs / plans** | `docs/specs/`, `docs/superpowers/` | Intention de conception au moment T (historique) | « comment on a conçu la feature ? » |
| **Recherche** | `docs/research/` | Explorations, hypothèses | « qu'a-t-on exploré ? » |

Aujourd'hui, `architecture.md` (§1-13) fait déjà une bonne part de la
**Reference** ET de l'**Explanation**. Le plan : les sujets bien couverts par
`architecture.md` restent là ; les **trous** ci-dessous obtiennent une page
`reference/` dédiée, et on crée les **runbooks** (`how-to/`) qui manquent
entièrement.

---

## Carte de couverture

Légende : ✅ couvert · 🟡 partiel / dispersé / potentiellement périmé · ❌ trou.

### Cœur runtime & décision
| Sous-système | Rôle | Réf | Où | Décisions |
|---|---|---|---|---|
| Orchestration du cycle | `daemon.py` : réveil → contexte → décision → gate → exécution → log | ✅ | archi §2-3 | D7 |
| Sélection des symboles dus | veilles + réveils temps/indicateur → `decidable` | ✅ | archi §3.1, §7 | D7, D9, D10 |
| Batch LLM / planificateur | `application/planner_batch` : budget modèle, tournée outils, REQUEST_CONTEXT | ✅ | archi §3.6 | D7 |
| Contexte marché (snapshot) | `application/market_snapshot` : barres, fraîcheur, FX, eligibility | ✅ | archi §3.2-3.3 | — |
| Enregistrement décision | `application/decision_recorder` → `state/decisions.jsonl` | ✅ | archi §3.8, §8 | — |
| Gate de pertinence (coût) | `relevance_gate` : quiet-gate, économie d'appels | ✅ | archi §3.4 | D7A |

### Actions & exécution
| Sous-système | Rôle | Réf | Où | Décisions |
|---|---|---|---|---|
| **Risk gate** | fusible déterministe pré-`broker.submit` (clamp 1 %, hard_stop, order_value, gross) | 🟡 | dispersé (archi §3.7) | — |
| Plans armés (EXECUTE_ORDER) | armé par le LLM, exécuté sans re-appel ; 5 garde-fous + fall-through LLM | ✅ | archi §3.5, §4.3 ; registre | D7B, D11, D12 |
| Admission d'ordre | `application/order_admission` : intent, clamp, stop, risk metrics | 🟡 | archi §3.7 | — |
| Allocateur budget gross | `gross_priority` : priorisation sous plafond d'exposition | 🟡 | spec 06-30 | — |
| Exécution / broker | `tools/execution`, `tools/portfolio` | 🟡 | archi §3.8 | — |
| Sorties automatiques | `exit_engine` : hard_stop, trailing, triggers | ✅ | archi §4 | — |
| Stops & résolution au tir | `resolve_exit_plan` : late-binding, structural/vol/percent | ✅ | archi §5 | D11 |

### Données
| Sous-système | Rôle | Réf | Où | Décisions |
|---|---|---|---|---|
| Sources marché & fraîcheur | `tools/data_source`, `tools/market`, `tools/ib_source` | ✅ | archi §3.2 | — |
| **Conversion FX** | `fx`, `fx_rates` : base USD, taux au fill | 🟡 | mention archi §3.3 ; spec 06-24 | chantier FX |
| Fil d'actu (news) | `tools/news_feed` : Yahoo, phase attribution | 🟡 | archi §13 ; spec 06-23 | — |
| Macro | `macro_calendar`, `macro_series` : FOMC/CPI, séries | 🟡 | archi §13 ; spec 07-02 | — |
| Cycle de vie / rotation | `ledger_rotation`, `decision_ledger`, archives | ✅ | archi §12 | — |
| État persistant | `state/*.jsonl`, `trade_plans.json`, `scheduler.json` | ✅ | archi §8 | — |

### Univers & régime
| Sous-système | Rôle | Réf | Où | Décisions |
|---|---|---|---|---|
| Radar / rotation / hot-sets | `radar*`, rotation par venue | ✅ | archi §6 | D9, D10 |
| Régime familial | `family_regime` : biais par univers | 🟡 | — | D2 |
| Config univers & portefeuille | `universe.yaml`, `portfolio_config`, `pool_config` | ❌ | — | — |

### LLM & agent
| Sous-système | Rôle | Réf | Où | Décisions |
|---|---|---|---|---|
| Transport LLM / acpx | `llm` : one-shot, fallback sonnet/ollama, reap ponts | ✅ | archi §9.1-9.2 | — |
| Contrat / protocole | `agent_protocol/` (types, prompts, parsing) + `codex_client` façade | 🟡 | archi §9.3 | — |
| Outils domaine (read-only) | `agent_tools/` : 9 handlers, tournée bornée | ✅ | archi §10 ; spec 06-29 | — |
| Mémoire / recall / learnings | `learnings_store`, `consolidator`, `embeddings`, `tools/memory` | ✅ | archi §11 ; spec 07-02 | — |
| Couche sémantique | `semantic/catalog` : familles, requêtes temporelles | 🟡 | — | — |

### Observabilité
| Sous-système | Rôle | Réf | Où | Décisions |
|---|---|---|---|---|
| Cockpit TUI | `cockpit*`, `ui/rich_panels`, `read_models`, `palette` | 🟡 | specs cockpit-v2 (chrono) | — |
| Attribution / performance | `attribution`, `meta_performance`, `decision_audit`, `decision_bench` | 🟡 | — | — |
| Logging | `logging_setup` : niveaux, `CASYS_LOG_LEVEL`, museler ib_async | ✅ | archi §10.1 | — |

### How-to / runbooks
| Runbook | Réf | Où |
|---|---|---|
| Déployer / relancer le daemon (superviseur, SIGINT hors-batch) | ❌ | — |
| Lire les logs (Gonzo `make logs`, `CASYS_LOG_LEVEL=DEBUG`) | ❌ | — |
| Mesurer le cœur swing (`scripts/measure_d7.py`) | ❌ | — |
| Rejouer des plans (`backtest/plan_replay.py`) | ❌ | — |

---

## Trous prioritaires (backlog)

Du plus risqué au moins risqué :

1. **Risk gate — réf unique** 🟡→✅. C'est le fusible de sécurité avant tout ordre réel ; aujourd'hui dispersé. Une page `reference/risk-gate.md` qui liste chaque contrôle, son seuil, son code de rejet.
2. **Conversion FX** 🟡→✅. A déjà causé un incident (sizing aveugle aux devises, plancher commission). Une réf `reference/fx.md` : base USD, où le taux s'applique, ce qui reste natif.
3. **Runbooks how-to** ❌. Aucun aujourd'hui — déploiement, logs, mesure, replay. Faible risque mais gros gain de friction (surtout pour un agent qui reprend le contexte).
4. **Cockpit & attribution** 🟡. Specs chronologiques, pas de réf « ce qu'affiche/mesure le cockpit aujourd'hui ».
5. **Config univers/portefeuille** ❌. Les `*.yaml` pilotent le comportement sans page qui les décrit.

On remplit dans cet ordre, une page à la fois.
