# Plan — fill outcome accounting

## Objectif

Réduire le bloc post-fill de `trader/runtime/daemon.py` en sortant la partie
accounting déterministe vers un service applicatif testable.

## Tranche

- Ajouter `trader/application/fill_outcome.py`.
- Déplacer le libellé déterministe des sorties LLM (`llm_exit`) vers ce service.
- Construire le payload `model_performance` et enrichir l'entrée décision
  (`model_performance_logged`, commission, devise, modèle, fx) hors du daemon.
- Garder dans le daemon le snapshot portefeuille et l'écriture durable
  `RuntimeStateWriter.append_model_performance`.
- Ajouter un garde de layout empêchant le retour du mapping commission/fx inline
  dans le bloc post-fill du daemon.

## Hors périmètre

- Ne pas déplacer le `RiskGate`.
- Ne pas changer le fallback broker synchrone.
- Ne pas déplacer encore la synchronisation `TradePlan` post-fill.
