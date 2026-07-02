# Référence — Reporting : attribution, audit, performance

> **Type** : Reference (Diátaxis).
> **Code** : `trader/reporting/` · **Rôle** : relier trades ↔ décisions, auditer, mesurer.

Tout est **ex-post et lecture-seule** sur `state/decisions.jsonl`, `history.jsonl`,
l'état broker. Jamais dans le hot-path de décision.

## Modules

| Module | Rôle |
|---|---|
| `attribution` | Relie **chaque trade clôturé à sa décision d'entrée** (thèse, contexte, confiance). Répond à « pourquoi on a pris ce trade ? ». |
| `decision_audit` | **Audit ex-post** des décisions loggées (classification, cohérence, cas anormaux). |
| `decision_bench` | **Bench contrefactuel** de modèles sur des lignes de décision auditées (compare des modèles a posteriori). |
| `meta_performance` | Payload **compact de méta-performance** pour l'agent runtime + le consolidateur (réinjecté au contexte). |
| `stats` | **KPI live** depuis l'historique d'équité + l'état du broker (affiché au cockpit). |
| `decision_ledger` | Journal durable des décisions (schéma versionné). |
| `decision_reason` | Vocabulaire des `decision_reason_code` (NO_EDGE, MARKET_CLOSED, ARMED_PLAN, EXIT_SIGNAL…). |
| `tool_trace` / `tool_usage` | Traces des tournées d'outils domaine du LLM. |

## Flux typiques

| Question | Chemin |
|---|---|
| « Quel trade vient de quelle thèse ? » | `attribution` (trade clôturé → `entry_decision_id`) |
| « Les décisions sont-elles cohérentes ? » | `decision_audit` |
| « Un autre modèle aurait-il mieux fait ? » | `decision_bench` (sur audit) |
| « KPI live (equity, P&L, win rate) ? » | `stats` → cockpit |
| « L'agent voit-il sa propre perf passée ? » | `meta_performance` → contexte LLM |

## Codes de raison de décision (`decision_reason`)

Enum stable posé sur chaque décision (`decision_reason_code`) : `NO_EDGE`,
`MARKET_CLOSED`, `POSITION_MANAGEMENT`, `WATCH_ARMED`, `DATA_STALE`, `EXIT_SIGNAL`,
`ARMED_PLAN`, `FEES_TOO_HIGH`, `CONFLICTING_SIGNALS`, etc. Sert au filtrage et à
l'audit (distinguer un HOLD infra d'un HOLD authored par le LLM).

## Voir aussi
- [Cockpit](cockpit.md) (consomme `stats`, `attribution`) · Architecture §11 (recall).
