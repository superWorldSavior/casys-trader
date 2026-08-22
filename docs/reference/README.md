# Références canoniques

> **Type** : Reference (Diátaxis).
> Ces pages décrivent le comportement actuel du système. Après livraison d'un
> plan ou d'une spec, c'est ici que le comportement runtime doit être consolidé.

## Runtime & décision

| Sujet | Page | Source principale |
|---|---|---|
| Orchestration, timers, veilles, plans armés | [`wake-scheduler.md`](wake-scheduler.md) | `planning/scheduler`, `planning/indicator_watch`, `runtime/cycle_scheduling` |
| File de tâches durable | [`task-queue.md`](task-queue.md) | `infrastructure/queue`, `runtime/queue_runtime` |
| Gouvernance et preuve du cycle paper | [`process-governance.md`](process-governance.md) | `domain/process_trace`, `runtime/process_*`, `process_event_store` |
| Exécution, broker, admission, budget | [`execution.md`](execution.md) | `application/execute/order_admission`, `execution/*` |
| Risk gate | [`risk-gate.md`](risk-gate.md) | `execution/risk.py`, `config/risk.yaml` |
| Reporting, audit, attribution, ledgers | [`reporting.md`](reporting.md) | `reporting/*` |
| World Model shadow (marché, `NO_GO`) | [`world-model.md`](world-model.md) | `application/world_model`, `reporting/read_models/world_*`, `state/world_model.db` |

## LLM & agent

| Sujet | Page | Source principale |
|---|---|---|
| Contrat prompts / JSON / mandat | [`llm-contract.md`](llm-contract.md) | `agent/protocol`, `mandate/` |
| Presets des cinq rôles LLM | [`model-presets.md`](model-presets.md) | `scripts/model_preset.py`, `ops/model-presets` |
| CODEX_HOME isolé du daemon | [`codex-home-isole.md`](codex-home-isole.md) | `ops/codex-home`, `support/system/process_env` |
| Domain tools et action tools | [`agent-tools.md`](agent-tools.md) | `agent/tools`, parser/compiler |
| Contexte agent | [`agent-context.md`](agent-context.md) | `agent/context` |
| Mémoire learnings / RAG | [`learnings-rag.md`](learnings-rag.md) | `agent/learnings`, `state/learnings*` |
| Architecture de connaissance agent | [`agent-knowledge-architecture.md`](agent-knowledge-architecture.md) | cadre stable de connaissance |
| Mémoire de situation (bac ②) | [`situation-memory.md`](situation-memory.md) | `state/situation_memory.db`, `state/news_briefs/` |
| Couche sémantique | [`semantic.md`](semantic.md) | `domain/semantic`, indicateurs gouvernés |

## Marché, données & config

| Sujet | Page | Source principale |
|---|---|---|
| Configuration | [`config.md`](config.md) | `config/*.yaml`, `support/config` |
| Univers, radar, challengers, agent univers | [`universe-rotation.md`](universe-rotation.md) | `market/rotation`, `market/radar*`, `domain/universe` |
| Pipeline Univers global/régional → Trader | [`universe-trader-pipeline.md`](universe-trader-pipeline.md) | `runtime/universe_intelligence_runtime`, `domain/universe/mandate` |
| Intelligence entreprise par symbole | [`company-intelligence.md`](company-intelligence.md) | `runtime/company_intelligence_runtime`, `infrastructure/market_sources/company` |
| Régime | [`regime.md`](regime.md) | `domain/market/regime`, `domain/market/family_regime` |
| FX | [`fx.md`](fx.md) | `domain/market/fx`, `infrastructure/market_sources/fx_rates` |
| News, scout challengers, analyste | [`news.md`](news.md) | `infrastructure/market_sources/news_feed`, `runtime/news_*` |
| Macro | [`macro.md`](macro.md) | `market/macro_*`, `runtime/news_macro_runtime` |

## Surfaces opérateur

| Sujet | Page | Source principale |
|---|---|---|
| Cockpit TUI et galerie Reports | [`cockpit.md`](cockpit.md) | `interfaces/cockpit`, `reporting/read_models/runtime_state` |

Règle de maintenance : une référence fausse est pire qu'une référence absente.
Lire le code courant, vérifier les champs d'état, puis seulement éditer la page.
