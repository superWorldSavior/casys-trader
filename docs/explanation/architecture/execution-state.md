# Architecture — exécution, sorties et état

> **Type** : Explanation (Diataxis). Retour à l'[index Explanation](../README.md).

## Chemins de sortie

Le daemon traite les sorties avant les nouvelles entrées. L'`exit_engine`
évalue les plans ouverts sur des barres suffisamment fraîches : hard stop,
take profit, trailing, protection du profit et temps maximal de détention. Ces
effets sont déterministes ; une sortie discrétionnaire LLM reste une décision
soumise aux mêmes admissions.

Un trigger de plan ou de veille peut réveiller le planificateur, mais ne doit
pas contourner les gardes. `resolve_exit_plan` transforme un plan armé en
intention contrôlée et garde une trace de la raison de résolution.

## Veilles et réveils

Les `indicator_watch` et `exit_watch` permettent de dormir entre les cycles.
Le scanner les évalue sans LLM, applique le cooldown et retire une veille
déclenchée avant de rendre le symbole dû. Cette mécanique explique pourquoi un
daemon ne repasse pas forcément tout l'univers à chaque poll.

## État persistant

`state/casys.db` est l'état paper canonique : broker simulé, scheduler, plans,
outbox et projections nécessaires à une reprise cohérente. Les ledgers JSONL
append-only restent des preuves ou sources spécialisées ; ils ne concurrencent
pas l'autorité transactionnelle SQLite.

| Artefact | Rôle |
|---|---|
| `casys.db` | broker, plans, scheduler, tâches d'exécution et projections runtime |
| `decisions.jsonl` | journal d'audit des décisions et provenance |
| `history.jsonl` / rapports | lecture opérateur et tendances de cycles |
| `process_events` | admissions, tentatives, effets et clôtures rejouables |
| `situation_memory.db` | index reconstruit des briefs, pas une source de vérité |

La rotation et les archives conservent la preuve utile sans faire du cache une
autorité. Les détails de schéma sont dans [task queue](../../reference/task-queue.md),
[reporting](../../reference/reporting.md) et
[mémoire de situation](../../reference/situation-memory.md).
