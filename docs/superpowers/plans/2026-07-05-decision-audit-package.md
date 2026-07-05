# Plan — decision audit package

## Objectif

Sortir le moteur d'audit ex-post du plat `reporting/` vers un sous-package
métier, sans casser les imports historiques.

## Tranche

- Ajouter `trader/reporting/audit/decision_quality.py`.
- Garder `trader/reporting/decision_audit.py` comme façade publique.
- Rebrancher les consommateurs internes (`runtime.cli`, `decision_bench`,
  `read_models.meta_performance`) sur le moteur canonique.
- Ajouter un garde layout prouvant que la façade réexporte bien le moteur
  canonique.

## Hors périmètre

- Ne pas modifier la sémantique d'audit ni le format `decision_audit.json`.
- Ne pas découper `decision_bench.py` dans cette tranche.
- Ne pas déplacer les commandes opérateur.
