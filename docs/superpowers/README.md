# Specs et plans historiques

> **Type** : Specs / plans.
> Ce dossier garde les intentions de conception et les plans d'exécution. Après
> livraison, la vérité courante doit être consolidée dans `docs/reference/`.

## Dossiers

| Dossier | Rôle |
|---|---|
| [`specs/`](specs/) | Design au moment T, arbitrages et exploration avant implémentation |
| [`plans/`](plans/) | Plans d'exécution, refactors par tranche, checklist de livraison |

## Règles

- Une spec peut être dépassée par le code livré.
- Un plan coché n'est pas une référence runtime.
- Toute livraison substantielle doit renvoyer vers une page `reference/`,
  `how-to`, `decisions` ou `postmortems` selon sa nature.
- Les plans TUI/design restent dans ce dossier comme historique de chantier ;
  ils ne doivent pas être confondus avec la référence cockpit.

## RFCs actives — World Model

Ces trois RFCs prolongent D19 sans activer le runtime ni donner d'autorité de
trading au World Model :

1. [Flux macro source-only](specs/2026-08-23-world-model-macro-source-only-design.md)
   — faits exogènes, objets temporels et observations prouvées au cutoff.
2. [Cohorte prospective appariée](specs/2026-08-23-world-model-prospective-cohort-design.md)
   — manifeste immuable, agrégat de cycle de vie, lanes et protocole d'étude.
3. [Graphe temporel et hypothèses de patterns](specs/2026-08-23-world-model-graph-pattern-hypotheses-design.md)
   — ontologie V3, features bornées et outcomes prospectifs de chaînes.

Le [plan d'exécution native Grok](plans/2026-08-23-world-model-native-grok-workflow.md)
transforme ces lots en workflow fail-closed, sans lancer d'implémentation ni
activer le runtime.

Ordre d'exécution : le flux macro et l'ontologie peuvent être préparés en
parallèle ; l'étude formelle attend le producteur macro gelé ; la voie V3 et la
confirmation des patterns attendent le manifeste de cohorte. Les lots Grok de
chaque RFC sont des work packages, pas une preuve de livraison.

Entrées canoniques liées : [`../reference/`](../reference/),
[`../how-to/`](../how-to/), [`../decisions/`](../decisions/).
