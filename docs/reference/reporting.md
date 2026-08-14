# Référence — Reporting : attribution, audit, performance

> **Type** : Reference (Diátaxis).
> **Code** : `trader/reporting/` · **Rôle** : relier trades ↔ décisions, auditer, mesurer.
> **CLI canonique** : `python -m trader.interfaces.cli.stats`,
> `python -m trader.interfaces.cli.attribution`,
> `python -m trader.interfaces.cli.tool_usage`.

Le reporting est **ex-post et lecture-seule**, chacun sur sa source :
`reporting/read_models/attribution.py` lit `model_performance.jsonl` ;
`reporting/read_models/live_kpis.py` projette les KPI et
`reporting/renderers/live_kpis.py` les rend pour l'opérateur ;
`reporting/read_models/tool_usage.py` projette l'usage des outils et
`reporting/renderers/tool_usage.py` le rend ; `reporting/read_models/meta_performance.py`
lit `decision_audit.json`. Les anciens modules `reporting.decision_ledger` et
`reporting.ledger.decision_ledger` ne sont plus des write-sides : ce sont des
façades de compatibilité vers la projection applicative et l'adaptateur JSONL.
Ces read models peuvent alimenter le contexte de décision (KPI, attribution,
méta-performance), mais restent strictement en lecture : aucun module sous
`reporting/` n'admet un ordre, ne mute le portefeuille ni ne clôt un processus.

## Modules

| Module | Rôle |
|---|---|
| `read_models.attribution` / `attribution` | Read model canonique des **round-trips** (trades clôturés) depuis `model_performance.jsonl`, rattachés au plan via `source_plan_id`; `reporting.attribution` garde la façade/rendu et la CLI canonique vit dans `interfaces.cli.attribution`. |
| `audit.decision_quality` / `decision_audit` | Moteur canonique d'**audit ex-post** des décisions loggées (classification, cohérence, cas anormaux); `reporting.decision_audit` garde la façade de compatibilité. |
| `bench.decision_bench` / `decision_bench` | Moteur canonique de **bench contrefactuel** de modèles sur des lignes de décision auditées; `reporting.decision_bench` garde la façade de compatibilité. Deux contrats : `reviews` (défaut sûr, avis BUY/SELL/HOLD) et `production` (JSON live `decisions[].calls`, grammaire `_SYMBOL_CALLS_FINAL_CONTRACT`). Le score reste BUY/SELL/HOLD (`strategy_close` → SELL). Un audit dont la plus récente `cycle_ts` a plus de 7 jours pose `audit_stale` / `audit_as_of` ; rafraîchir avec `casys-trader decisions audit`, ne pas réécrire le fichier à la main. |
| `read_models.meta_performance` / `meta_performance` | Read model canonique du payload **compact de méta-performance** depuis `decision_audit.json`; `reporting.meta_performance` garde la façade de compatibilité. |
| `read_models.live_kpis` / `renderers.live_kpis` / `stats` | Projection canonique des **KPI live** depuis `state/`, puis rendu opérateur; `reporting.stats` garde la façade de compatibilité et la CLI canonique vit dans `interfaces.cli.stats`. |
| `ledger.decision_ledger` / `decision_ledger` | Façades historiques vers `application.record.decision_ledger_rows` (projection pure), `domain.decision_identity` (identité stable) et `infrastructure.files.decision_ledger` (JSONL, seed, backfill). |
| `decision_reason` | Façade de compatibilité vers `trader.domain.decision_reason`, vocabulaire canonique des `decision_reason_code`. |
| `tool_trace` / `read_models.tool_usage` / `renderers.tool_usage` / `tool_usage` | Traces des tournées d'outils domaine du LLM ; projection canonique dans `reporting/read_models/tool_usage.py`, rendu dans `reporting/renderers/tool_usage.py`, façade dans `reporting.tool_usage`. |

## Flux typiques

| Question | Chemin |
|---|---|
| « Quel trade vient de quel plan ? » | `attribution` (round-trip → `source_plan_id`) |
| « Les décisions sont-elles cohérentes ? » | `decision_audit` |
| « Un autre modèle aurait-il mieux fait ? » | `decision_bench` (sur audit) |
| « KPI live (equity, P&L, win rate) ? » | `stats` → cockpit |
| « L'agent voit-il sa propre perf passée ? » | `read_models.meta_performance` → contexte LLM |

Les anciens raccourcis `python -m trader.stats`, `python -m trader.attribution`
et `python -m trader.tool_usage`, ainsi que `python -m trader.commands.*`,
restent supportés via les alias virtuels de `trader/__init__.py`, mais ils
délèguent aux modules canoniques de `trader/interfaces/cli/`.

## Projection de corrélation du processus

`application.record.decision_ledger_rows.build_decision_row()` ajoute un objet
`process` seulement si le runtime a fourni une identité ou une version de
gouvernance :

```json
{
  "process": {
    "process_instance_id": "…",
    "attempt_id": "…",
    "runtime_run_id": "…",
    "decision_id": "…",
    "governance_version": {"bundle_sha256": "…"}
  }
}
```

Le `decision_id` projeté est l'identité durable résolue de la ligne. Le
`governance_version` contient aussi, dans le runtime intégré, la liste des
artefacts allowlistés et leurs empreintes. La décision originale reste sous
`decision`, avec les effets et readbacks déjà produits avant son écriture ; le
`decision_readback` est créé ensuite par la relecture de cette ligne.

Le daemon relit cette ligne durable avant toute clôture du processus et compare
exactement le symbole, le `decision_id` et les trois IDs de processus. Les
jointures d'audit disponibles sont donc :

| Source | Clé de jointure | Vérité portée |
|---|---|---|
| `decisions.jsonl` | `decision_id` + objet `process` | décision, version de gouvernance, reçus déclarés |
| `process_events` dans `casys.db` | `process_instance_id`, `attempt_id`, `decision_id` dans les `effect_refs` | lifecycle append-only et résultat terminal éventuel |
| `broker_fills` dans `casys.db` | `process_instance_id`, `attempt_id`, `decision_id` | effet paper effectivement persisté |

Le reporting reste lecture-seule : il peut projeter ces liens, mais il ne doit
ni inférer un `completed` depuis une décision seule, ni réparer ou clore une
instance. Le résultat terminal autoritatif est l'événement
`process_events` portant `event_type=instance_closed` ; en son absence,
l'instance demeure ouverte.

## Codes de raison de décision (`domain.decision_reason`)

Enum stable (15 codes, `trader/domain/decision_reason.py`) posé sur chaque
décision (`decision_reason_code`) : `NO_EDGE`, `MARKET_CLOSED`,
`POSITION_MANAGEMENT`, `WATCH_ARMED`, `DATA_STALE`, `EXIT_SIGNAL`,
`ENTRY_SIGNAL`, `ARMED_PLAN`, `FEES_TOO_HIGH`, `CONFLICTING_SIGNALS`,
`WAITING_PULLBACK`, `RISK_LIMIT`, `ALREADY_EXPOSED`, `POST_LOSS_CAUTION`,
`UNKNOWN`. Sert au protocole LLM, au filtrage et à l'audit (distinguer un HOLD
infra d'un HOLD authored par le LLM).

`trader.reporting.decision_reason` reste importable comme façade legacy, mais
les imports internes doivent viser `trader.domain.decision_reason`.

## Voir aussi
- [Gouvernance du processus](process-governance.md) (identités, preuves et
  clôture) · [Exécution](execution.md) (propagation ordre/fill) ·
  [Cockpit](cockpit.md) (consomme `stats`, `attribution`) · Architecture §11
  (recall).
