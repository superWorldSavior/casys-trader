# Plan - decision ledger package

## Objectif

Sortir le write-side durable du ledger decision du plat `reporting/` vers un
sous-package dedie, sans casser les imports historiques.

## Tranche

- Ajouter `trader/reporting/ledger/decision_ledger.py`.
- Garder `trader/reporting/decision_ledger.py` comme facade publique.
- Rebrancher les consommateurs internes (`daemon`, bootstrap, CLI,
  `DecisionRecorder`) sur le store canonique.
- Ajouter un garde layout prouvant que la facade reexporte bien le store
  canonique.

## Hors perimetre

- Ne pas modifier le schema ni le format `decisions.jsonl`.
- Ne pas changer la logique de dedup/cache/backfill.
- Ne pas deplacer les commandes operateur.
