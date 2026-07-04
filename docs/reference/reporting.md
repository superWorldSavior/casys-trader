# Référence — Reporting : attribution, audit, performance

> **Type** : Reference (Diátaxis).
> **Code** : `trader/reporting/` · **Rôle** : relier trades ↔ décisions, auditer, mesurer.
> **CLI** : `python -m trader.commands.stats`, `python -m trader.commands.attribution`,
> `python -m trader.commands.tool_usage`.

Presque tout est **ex-post et lecture-seule**, chacun sur sa source : `attribution`
lit `model_performance.jsonl` ; `stats` calcule/rend les KPI projetés par
`read_models/live_kpis.py` ; `meta_performance` lit `decision_audit.json`.
**Exception** : `decision_ledger`
**écrit** — c'est lui qui PRODUIT `decisions.jsonl` (`append`/`replace_all`/`seed`).
Rien n'est dans le hot-path de décision.

## Modules

| Module | Rôle |
|---|---|
| `attribution` | Reconstruit les **round-trips** (trades clôturés) depuis `model_performance.jsonl`, rattachés au plan via `source_plan_id`. Base de l'analyse « pourquoi ce trade ? ». |
| `decision_audit` | **Audit ex-post** des décisions loggées (classification, cohérence, cas anormaux). |
| `decision_bench` | **Bench contrefactuel** de modèles sur des lignes de décision auditées (compare des modèles a posteriori). |
| `meta_performance` | Payload **compact de méta-performance** pour l'agent runtime + le consolidateur (réinjecté au contexte). |
| `stats` | Helpers de reporting des **KPI live** calculés par `read_models/live_kpis.py`; la CLI canonique vit dans `commands.stats`. |
| `decision_ledger` | Journal durable des décisions (schéma versionné). |
| `decision_reason` | Vocabulaire des `decision_reason_code` (NO_EDGE, MARKET_CLOSED, ARMED_PLAN, EXIT_SIGNAL…). |
| `tool_trace` / `tool_usage` | Traces des tournées d'outils domaine du LLM. |

## Flux typiques

| Question | Chemin |
|---|---|
| « Quel trade vient de quel plan ? » | `attribution` (round-trip → `source_plan_id`) |
| « Les décisions sont-elles cohérentes ? » | `decision_audit` |
| « Un autre modèle aurait-il mieux fait ? » | `decision_bench` (sur audit) |
| « KPI live (equity, P&L, win rate) ? » | `stats` → cockpit |
| « L'agent voit-il sa propre perf passée ? » | `meta_performance` → contexte LLM |

Les anciens raccourcis `python -m trader.stats`, `python -m trader.attribution`
et `python -m trader.tool_usage` restent supportés, mais ils délèguent aux
modules canoniques de `trader/commands/`.

## Codes de raison de décision (`decision_reason`)

Enum stable (15 codes, `decision_reason.py`) posé sur chaque décision
(`decision_reason_code`) : `NO_EDGE`, `MARKET_CLOSED`, `POSITION_MANAGEMENT`,
`WATCH_ARMED`, `DATA_STALE`, `EXIT_SIGNAL`, `ENTRY_SIGNAL`, `ARMED_PLAN`,
`FEES_TOO_HIGH`, `CONFLICTING_SIGNALS`, `WAITING_PULLBACK`, `RISK_LIMIT`,
`ALREADY_EXPOSED`, `POST_LOSS_CAUTION`, `UNKNOWN`. Sert au filtrage et à l'audit
(distinguer un HOLD infra d'un HOLD authored par le LLM).

## Voir aussi
- [Cockpit](cockpit.md) (consomme `stats`, `attribution`) · Architecture §11 (recall).
