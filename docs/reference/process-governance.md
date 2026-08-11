# Référence — Gouvernance et preuve du cycle de décision

> **Type** : Reference (Diátaxis).
> **Code** : `config/process_governance.yaml`,
> `support/metadata/governance_version.py`, `domain/process_trace.py`,
> `runtime/process_lifecycle.py`, `runtime/process_pilot.py`,
> `infrastructure/state_db/process_event_store.py`
> **Rôle** : définir l'instance de processus observée, sa version de gouvernance
> et les preuves nécessaires pour relier une décision paper à ses effets.

Le processus intégré est `casys-trader.paper-decision-cycle`, version `0.1`.
Son objet de travail est un symbole paper dû, ou une position ouverte à revoir.
Une instance commence quand cet objet et son contexte observable sont
disponibles. Elle ne peut être terminée que lorsque la décision est durable et
que chaque effet demandé est relu dans son store autoritatif, rejeté de façon
déterministe, ou explicitement laissé en récupération.

## Périmètre

Le pilote couvre le chemin **symbole dû → décision durable → effets paper
observés**. Il ne couvre pas :

- la sélection et la rotation d'univers ;
- la production des analyses macro, news ou entreprise ;
- les backtests et le développement de stratégie ;
- la consolidation des learnings ou l'évolution inter-instance du processus.

La responsabilité déclarée dans `config/process_governance.yaml` est
`Casys Trader Process Owner`, assignée à Erwan Lee Pesle et acceptée le
2026-08-09.

## Sémantique du pilote

Le daemon construit un unique `ProcessPilot` après la revendication du PID et
le bootstrap de l'état. Le pilote est une **couche d'observation** : il attache
des identités et des preuves au traitement déjà admis, sans retirer de symbole
du dispatch et sans décider si le LLM doit être appelé. Les files de tâches, le
broker et leurs règles d'idempotence restent propriétaires du comportement
métier.

Une interruption après l'admission, sans référence vers un effet engageant, ne
prouve donc pas un effet inconnu. Le symbole reprend la même instance avec une
nouvelle tentative. `recovery_required` n'est repris que lorsqu'un événement
ouvert conserve de vraies références causales à réconcilier.

## Identités

| Champ | Portée | Règle |
|---|---|---|
| `process_instance_id` | objet de travail | stable tant que l'instance reste ouverte ; une nouvelle admission après clôture reçoit un nouvel ID |
| `attempt_id` | tentative | renouvelé à chaque reprise ou nouvelle tentative de la même instance |
| `runtime_run_id` | processus daemon | créé une fois après la prise du PID ; un run sert plusieurs instances et tentatives |
| `decision_id` | décision durable | joint la tentative à la ligne de décision et aux effets d'exécution |
| `event_id` | événement de processus | clé d'idempotence append-only |

En mode file, les trois identités de processus traversent la tâche `decide` et
son résultat. Le bundle complet de gouvernance n'est pas copié dans chaque
payload : il reste épinglé sur l'événement de processus et sur la décision
durable.

## Version de gouvernance

`current_governance_version()` valide le contrat YAML puis calcule
`bundle_sha256` à partir d'une allowlist fixe :

- `config/process_governance.yaml` ;
- `mandate/mandate.md` et `mandate/memory.md` ;
- `config/risk.yaml` ;
- `trader/agent/protocol/prompts.py` et
  `trader/agent/protocol/llm_schema.py` ;
- `mandate/guardrails.json`, s'il existe.

Chaque entrée contient son chemin, son SHA-256 et sa taille. Les symlinks, les
sorties du dépôt et les fichiers non réguliers sont refusés. La configuration
ne peut pas ajouter arbitrairement un fichier à l'empreinte ; les fichiers
d'environnement et les secrets ne sont jamais lus.

Une instance ouverte reste épinglée à son `process_version` et à son
`bundle_sha256`. Un runtime portant un autre bundle ne peut pas la reprendre
silencieusement : `OpenProcessVersionMismatch` fait échouer la reprise.

## Journal durable

`ProcessEventStore` écrit dans la table SQLite `process_events` de
`state/casys.db`. Les événements sont immuables et relus par
`process_instance_id` dans l'ordre `seq`.

| `event_type` | Signification |
|---|---|
| `instance_admitted` | première admission de l'objet de travail |
| `attempt_started` | nouvelle tentative d'une instance encore ouverte |
| `attempt_finished` | tentative différée ou placée en récupération, sans résultat terminal |
| `instance_closed` | résultat terminal porté par un append atomique |

Rejouer le même `event_id` avec le même contenu est un no-op. Réutiliser cet ID
avec un contenu différent lève `ProcessEventConflictError`. Aucun événement ne
peut être ajouté après `instance_closed`.

Les événements transportent les causes d'admission, les identités, les pins de
version, `outcome_code`, `effect_status` et les `effect_refs`. Les causes
actuelles sont les déclencheurs d'indicateur, les raisons de réveil, ou
`scheduled_due` en l'absence d'un signal plus précis.

## Corrélation décision → effets

La chaîne de corrélation possible est :

```text
process_instance_id
  └─ attempt_id
       └─ decision_id
            ├─ decision_readback
            ├─ schedule_readback / plan_readback
            ├─ execute_task
            └─ broker_fill / portfolio_readback
```

Avant de tenter la clôture, le daemon écrit la décision, la relit par
`decision_id`, puis vérifie exactement `symbol`, `decision_id`,
`process_instance_id`, `attempt_id` et `runtime_run_id`. Un simple ID encore en
mémoire n'est pas une preuve de persistance.

`closure_evidence_complete()` exige ensuite :

- un `decision_readback` exact et vérifié ;
- un `schedule_effect` vérifié ;
- chaque effet de plan demandé relu et vérifié ;
- pour une exécution paper, un `broker_fill` dont `symbol`, les IDs de processus
  et `decision_id` correspondent exactement ;
- pour un dry-run, un reçu explicite `execution_mode=dry_run` avec
  `effect_status=not_applied_dry_run`.

Une admission d'exécution sans fill vérifié ne devient jamais un succès par
déduction. Un fill absent ou mal corrélé produit `effect_status=unknown` et
`process_state=recovery_required`.

## Résultats et limite actuelle

Le contrat déclare quatre résultats terminaux : `completed`, `failed`,
`escalated` et `cancelled`. `recovery_required` est explicitement non terminal.

Le lifecycle intégré clôt aujourd'hui automatiquement uniquement
`completed`, quand toutes les preuves minimales et leur corrélation sont
valides. Cette clôture tient dans un unique événement `instance_closed`, ce qui
évite une fenêtre de crash entre « tentative finie » et « instance close ».

Les protocoles de réconciliation causale après un effet inconnu ne sont pas
encore implémentés. `close_after_recovery()` échoue volontairement avec
`NotImplementedError`, même face à un reçu qui semble correctement formé : les
références non résolues restent attachées à l'instance ouverte plutôt que de
fabriquer un résultat terminal.

## Voir aussi

- [Exécution](execution.md) — propagation des IDs jusqu'à l'ordre et au fill.
- [Reporting](reporting.md) — projection read-only des jointures de processus.
- [File de tâches](task-queue.md) — idempotence et retries des tâches métier.
