# Plan — meta-performance read model

## Objectif

Renforcer le CQRS léger côté reporting : la méta-performance devient un read
model canonique, et `reporting.meta_performance` reste une façade de
compatibilité.

## Tranche

- Ajouter `trader/reporting/read_models/meta_performance.py`.
- Déplacer `compute_meta_performance`, `DEFAULT_HORIZONS` et le cache mtime dans
  ce read model.
- Garder `trader/reporting/meta_performance.py` comme façade publique.
- Rebrancher les consommateurs internes (`daemon`, consolidateur) sur le read
  model canonique.
- Ajouter un garde layout prouvant que la façade réexporte bien la projection
  canonique.

## Hors périmètre

- Ne pas modifier la sémantique du payload méta-performance.
- Ne pas changer `decision_audit` ni `decision_bench`.
- Ne pas changer les CLI opérateur.
