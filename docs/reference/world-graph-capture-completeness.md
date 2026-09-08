# Référence — Complétude de capture du graphe World

> **Type** : Reference (Diátaxis).
> **État vérifié** : 2026-09-08.
> **Code** : `trader/domain/world_graph.py` ·
> `trader/infrastructure/graph/world_temporal_networkx.py` ·
> `trader/application/world_model/graph_snapshot.py` ·
> `trader/application/world_model/pattern_path.py`

Cette page décrit comment un snapshot graphe borné conserve l'ascendance
instrument → venue → pays → région → world, et comment la projection de
chemins de pattern refuse une ancestrie géographique tronquée par le budget.

## Vocabulaire canonique

Une seule séquence vit dans le domaine : `GEOGRAPHIC_ANCESTRY_WALK` et
`ROOT_BRANCH_WALK` (`trader/domain/world_graph.py`). Chaque hop est
`(kind, direction, target_kind)` et doit être permis par
`GRAPH_TRAVERSAL_V1_DIRECTIONS`. L'application et l'infrastructure
importent le même objet ; elles ne redéfinissent pas la table.

## Invariants

- Budget inchangé : `GRAPH_TRAVERSAL_MAX_PATHS = 32`, `GRAPH_TRAVERSAL_MAX_DEPTH = 4`.
- Directions, PIT et périmètre inchangés : seules les relations déjà admises
  par `GRAPH_TRAVERSAL_V1_DIRECTIONS` et effectives au cutoff sont visitées.
- Les membres du snapshot restent exactement les relations des chemins visités.
  Aucune relation non visitée n'est ajoutée pour « compléter » le graphe.
- Un snapshot `partial` / `graph_budget_exceeded` reste un artefact légitime
  (features, explorateur, overlay). Il n'est pas rejeté en bloc.

## Parcours borné

Le projecteur NetworkX n'est pas une autorité de persistance. Il énumère au
plus 32 chemins. L'expansion n'est plus un DFS ordonné uniquement par hash de
`relation_id` : `graph_expansion_priority` classe les hops, de façon
déterministe, avant le fan-out des instruments pairs de la place.

| Priorité | Hops |
|---|---|
| 0 | Ascendance avant : `TRADED_ON`→venue, `LOCATED_IN`→country, `LOCATED_IN`→region, `PART_OF_WORLD`→world |
| 1 | Branches racine : `ISSUED_BY`→company, `MEMBER_OF_FAMILY`→family |
| 2 | Overlays `OBSERVES` / `ABOUT` |
| 3 | Le reste, dont `TRADED_ON` inverse vers les instruments pairs |

À priorité égale, l'ordre reste `(relation_id, direction, to_id, préfixe)`.
Le même graphe, relations insérées dans l'ordre inverse, produit les mêmes
chemins. Une place dense (>32 pairs) marque toujours `graph_budget_exceeded`
si des hops restent hors budget ; l'ascendance utile est déjà dans les 32.

## Snapshot

`WorldGraphSnapshotService` n'invente pas de membres. Si le budget est
dépassé **et** que les membres visités n'incluent pas toute la chaîne
géographique **disponible** dans la révision, `missingness["ancestry"] = "incomplete"`
s'ajoute à `missingness["budget"] = "graph_budget_exceeded"`.

Si le budget est dépassé mais que la chaîne disponible (souvent les quatre
hops instrument→world) est présente, le snapshot reste `partial` **sans**
`ancestry=incomplete`. C'est un partial éligible pour un pattern géographique.

## Projection de chemins (contrat pour la découverte)

`project_pattern_paths` / `project_pattern_path_details` sont le contrat
partagé. La découverte et l'évaluation consomment cette projection ; elles
n'ont pas à re-filtrer le fan-out de place.

Une chaîne géographique autonome n'est émise que si :

1. elle a les quatre hops `instrument → venue → country → region → world`, ou
2. le snapshot n'est pas une capture tronquée par budget (`status=partial`,
   `missingness.budget=graph_budget_exceeded`, ou `missingness.ancestry=incomplete`).

Conséquences :

| Snapshot | Chaîne géographique | Pattern géographique |
|---|---|---|
| `complete`, 1 à 3 hops (graphe court réel) | incomplète au sens world | émise telle quelle |
| `complete` ou `partial`, 4 hops | complète | émise |
| `partial` budget, 2 ou 3 hops (cas TW 21/54) | tronquée par capture | **non émise** |
| `partial` budget, 4 hops + pairs hors budget | complète | émise |

Les overlays `OBSERVES` / `ABOUT` restent optionnels : une ancestrie complète
sans overlay est un chemin valide ; un overlay absent n'invalide pas le
chemin structurel. Les branches `ISSUED_BY` / `MEMBER_OF_FAMILY` restent
indépendantes. Un overlay ou une branche présents dans les membres d'une
capture partielle restent projetables ; ce n'est pas une signature
géographique profondeur 2/3.

Les snapshots historiques `partial` déjà persistés ne sont pas mutés. Relus
par la projection actuelle, ils ne redeviennent pas silencieusement des
signatures de profondeur 2/3.

## Hors périmètre

- Pas d'augmentation du budget.
- Pas d'édition de `pattern_discovery`, `pattern_evaluation`, workflow ou
  pipeline macro.
- `graph_traversal.v1` (directions autorisées) n'est pas bumpé : le
  changement est un ordre d'expansion dans le même contrat de hops.
- Les hypothèses déjà enregistrées sur des signatures tronquées restent
  immuables ; une nouvelle formation après captures corrigées est un lot
  découverte, pas un rewrite de ledger.

## Voir aussi

- [World Model](world-model.md)
- Diagnostic 2026-09-08 : troncature budget vs ledger profondeur 4
