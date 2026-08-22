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
| `application/world_model` | capture, labels à horizon fixe, baseline Markov et GRU shadow |

## Contexte borné World Model

Le World Model est un bounded context **marché**, isolé du Trader, du Brain et
de l'Univers. Il prédit `DOWN` / `FLAT` / `UP` à `elapsed_4h.v1` et
`elapsed_1d.v1` depuis un `WorldEpisode` action-free. Il ne contient jamais la
mémoire Brain ni Univers, ne conseille pas le planificateur, et reste
`shadow_only` / `NO_GO`.

```text
domain/world_episode          contrat d'observation et d'identité
        │
        ├─ application/world_model     capture, labeler, encoding, baseline, GRU, service
        ├─ infrastructure/state_db     world_model.db append-only + query lecture seule
        └─ reporting/read_models       évaluation, impact honnête, status projector
                ▲                              ▲
                │                              │
        runtime (adapter background     interfaces/cli (adaptateur mince)
        fail-open, façade compat)
```

Carte des relations : Univers sélectionne l'attention ; Brain décide ; le
code exécute ; FLAIR/D16/MemRL jugent leurs boucles. Le World Model n'entre
dans aucune de ces autorités. `state/world_model.db` n'est pas une table de
`casys.db`. Voir [world model shadow](world-model-shadow.md) et
[référence](../../reference/world-model.md).

La [file de tâches](../../reference/task-queue.md) et l'[état persistant](execution-state.md#etat-persistant) sont des détails de conception ; leurs contrats complets vivent en Reference.

## Pourquoi ce découpage

La séparation évite d'attribuer une décision technique au LLM : un `HOLD`
produit par une fraîcheur insuffisante ou un quiet gate est un fait
infrastructurel, pas un avis du modèle. Elle permet aussi de rejouer et auditer
un cycle sans confondre l'intention agent, l'admission de risque et le fill.

Voir aussi : [cycle de décision](decision-cycle.md),
[architecture de connaissance](../../reference/agent-knowledge-architecture.md)
et [registre des décisions](../../decisions/registre-decisions-metier.md).
