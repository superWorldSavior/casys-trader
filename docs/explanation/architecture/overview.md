# Architecture — vue d'ensemble

> **Type** : Explanation (Diataxis). Retour à l'[index Explanation](../README.md).

casys-trader est un daemon de trading paper. Le LLM planifie : il reçoit des
faits calculés et produit des artefacts structurés (décision, plan, veille). Le
code garde l'autorité sur les indicateurs, les admissions, les gates et les
effets persistants.

| Boucle | Acteur | But |
|---|---|---|
| Conception | Humain | mandat, guardrails, configuration d'univers |
| Runtime | Daemon + LLM | décider, exécuter en paper, prouver, se réveiller |

## Frontières qui comptent

```text
domain/          politiques et identités pures
application/     cas d'usage et contrats locaux
infrastructure/  SQLite, fichiers, broker paper, queue et fournisseurs
runtime/          composition, cycles, logs et arrêt
interfaces/       CLI et cockpit en lecture
agent/            protocole LLM, prompts, outils read-only et mémoires
```

Le `runtime/daemon.py` est le composition root : il peut assembler les effets
de bord, mais les modules métier ne remontent pas vers lui. Les façades
historiques existent pour la compatibilité ; les nouveaux imports visent les
owners canoniques.

## Modules structurants

| Zone | Responsabilité |
|---|---|
| `application/cycle` | snapshot marché, scope des décisions, holds infra, réveils et scans de veilles |
| `application/decide` | projection du contexte et dispatch de la décision symbole |
| `application/execute` / `application/exit` | admission, plans, fills et sorties déterministes |
| `application/record` | identité, ledger, rapports, traces d'outils et feedback |
| `infrastructure/queue` | tâches durables, leases, retries et backpressure |
| `infrastructure/state_db` | état paper SQLite, outbox et projections persistantes |
| `application/universe` | scopes candidats, activation et revalidation de la hotlist |

La [file de tâches](../../reference/task-queue.md) et l'[état persistant](execution-state.md#etat-persistant) sont des détails de conception ; leurs contrats complets vivent en Reference.

## Pourquoi ce découpage

La séparation évite d'attribuer une décision technique au LLM : un `HOLD`
produit par une fraîcheur insuffisante ou un quiet gate est un fait
infrastructurel, pas un avis du modèle. Elle permet aussi de rejouer et auditer
un cycle sans confondre l'intention agent, l'admission de risque et le fill.

Voir aussi : [cycle de décision](decision-cycle.md),
[architecture de connaissance](../../reference/agent-knowledge-architecture.md)
et [registre des décisions](../../decisions/registre-decisions-metier.md).
