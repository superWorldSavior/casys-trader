# Note de statut — Pilote World Model shadow (2026-08-24)

> **Type** : décision / statut (pas une réécriture du registre).
> **Lié à** : D19 dans [`registre-decisions-metier.md`](registre-decisions-metier.md)
>   (inchangé : bounded context marché, `shadow_only`, `NO_GO`).
> **RFCs** : conception 2026-08-23 (macro, cohorte, graphe) — cette note dit
>   *où en est le runtime*, pas ce que les RFCs auraient dû écrire.
> **Doc opérateur** : [`operate-world-model-shadow.md`](../how-to/operate-world-model-shadow.md).
> **Explanation** : [`world-model-shadow.md`](../explanation/architecture/world-model-shadow.md),
>   [`world-context-ontology.md`](../explanation/architecture/world-context-ontology.md).

## Décision

Autoriser un **pilote `pipeline_pilot` d'une semaine**, shadow-only, sur
l'état paper supervisé à partir du **2026-08-24 Asia/Taipei**. Le V1 World
Model et les ombres cohorte / macro source-only / graphe V3 démarrent
**ensemble selon la config commitée**. Aucune promotion Trader.

## Exception à l'activation RFC

Les RFCs exigent une activation **explicite** (`register` / `arm` /
`start` CLI). Le runtime ne transforme pas ça en défaut magique.

**Exception opérateur** : `config/world_shadow_pilot.yaml` avec
`activation_policy=operator_authorized_on_boot` et `enabled: true`. Ce
YAML versionné (`content_sha256`) **est** l'autorisation. Le flag
`CASYS_WORLD_SHADOW_PILOT_ACTIVATION` (défaut `1`) l'honore ; `=0` saute
register/arm/start et le OU des workers, le V1 reste.

Ce n'est pas un feu vert `prospective_evaluation`. Ce n'est pas une
activation implicite « parce que le code existe ».

## Fenêtre

Pas une date de fin déjà expirée dans le YAML (volontairement aucune
`2026-08-17`).

- calendrier supervisé : 2026-08-24, Asia/Taipei ;
- runtime : `window.planned_start=boot_event_time`, `duration_days=7`,
  `collection_stop_kind=fixed_end` ;
- premier boot réussi : `planned_start_not_before = now`,
  `collection_stop_at = now + 7d` ;
- boots suivants : idempotents, fenêtre **immobile**, zéro backfill.

## Ce qui démarre (config courante)

| Surface | Comment c'est allumé |
|---|---|
| V1 marché (Markov + GRU 12 pas) | `CASYS_WORLD_MODEL_SHADOW_ENABLED` défaut `1` |
| Contexte V2 | flag défaut `0` **OU** `workers.context_v2` du YAML si le pilote s'active |
| Macro source-only | flag défaut `0` **OU** `workers.macro_source_only` |
| Graphe V3 | flag défaut `0` **OU** `workers.graph_v3` |
| Cohortes C1 + graph_v3 | `activate_world_shadow_pilot` au boot V1 si activation on |

Deux cohortes, `study_kind=pipeline_pilot`, `authority=shadow_only`,
`decision_effect=none`, `recommendation=NO_GO`, `causal_claim=false`,
`pnl_claim=false` :

1. **technical_c1** — lanes Markov + GRU froid (`sequence_length=4`) :
   `market`, `status_only`, `company`, `macro`, `joint`. **Pas de graphe.**
2. **graph_v3** — `markov.graph` (`topology_status_only`) + `gru.graph`
   (`graph_content`). `config/world_graph_v3.yaml` garde `cohort_id: null` ;
   l'id est injecté au compose.

NetworkX = projection fraîche, jamais persistée, jamais autorité. Pas
d'arête `CAUSES`. Macro World Context = source-only ; **GDELT** et
**`NewsMacroBrief` exclus**.

Un boot câble ; un cycle idle peut n'écrire **aucun** épisode ; seules
les captures dues (ancre OHLCV valide) écrivent.

## Claims après une semaine

Autorisés : couverture, plomberie, tendances préliminaires.

Interdits : preuve causale, PnL Trader, drawdown de portefeuille, promotion
automatique, réinterprétation en `prospective_evaluation`.

Le rapport cohorte force
`interpretation_limit=coverage_plumbing_preliminary_trends_only` tant que
l'empan observé est inférieur à 8 jours ; `causal_claim` / `pnl_claim`
restent `false` ensuite.

## Arrêt

Voir le how-to. Surfaces : flag d'activation `0` (stoppe l'auto-start, pas
une cohorte déjà `collecting`), `world cohort close` / `invalidate`, flags
workers, `CASYS_WORLD_MODEL_SHADOW_ENABLED=0` (coupe store + cohortes ;
le worker macro reste possible tant que son flag/YAML est on). Le chemin
de décision Trader ne change pas.

## Hors périmètre (inchangé depuis D19 / RFCs)

Jointure pré-décision durable, politique d'ordres shadow, GNN / GraphRAG /
Neo4j, réentraînement Brain / Univers / FLAIR / MemRL, recyclage des
briefs Univers comme faits exogènes.
