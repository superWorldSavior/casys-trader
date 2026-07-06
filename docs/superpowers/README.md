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

Entrées canoniques liées : [`../reference/`](../reference/),
[`../how-to/`](../how-to/), [`../decisions/`](../decisions/).
