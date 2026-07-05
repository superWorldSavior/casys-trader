# Plan — decision bench package

## Objectif

Sortir le moteur de bench contrefactuel du plat `reporting/` vers un
sous-package dédié, sans casser les imports historiques.

## Tranche

- Ajouter `trader/reporting/bench/decision_bench.py`.
- Garder `trader/reporting/decision_bench.py` comme façade publique.
- Rebrancher `runtime.cli` et les tests comportementaux sur le moteur canonique.
- Ajouter un garde layout prouvant que la façade réexporte bien le moteur
  canonique.

## Hors périmètre

- Ne pas découper l'intérieur du bench dans cette tranche.
- Ne pas changer le format `last_decision_bench.json`.
- Ne pas changer les commandes opérateur.
