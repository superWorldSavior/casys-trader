# Plan — attribution read model

## Objectif

Renforcer le CQRS léger côté reporting : le calcul attribution devient un read
model canonique, et `reporting.attribution` reste une façade de compatibilité +
rendu texte.

## Tranche

- Ajouter `trader/reporting/read_models/attribution.py`.
- Déplacer le calcul `compute_round_trips`, `compute_attribution`,
  `compute_hard_stop_diagnostics` et `select_hard_stop_symbols` dans ce read model.
- Garder `trader/reporting/attribution.py` comme façade publique et owner du
  rendu `render_text`.
- Rebrancher les consommateurs internes de calcul (`daemon`, runtime state,
  consolidateur) sur le read model canonique.
- Ajouter un garde layout prouvant que la façade et l'alias legacy réexportent
  bien la projection canonique.

## Hors périmètre

- Ne pas modifier la sémantique attribution ni le format du payload.
- Ne pas changer les CLI opérateur.
- Ne pas déplacer `decision_audit` ou `decision_bench` dans cette tranche.
