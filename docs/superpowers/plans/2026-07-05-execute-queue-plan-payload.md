# Plan — execute queue plan payload

## Objectif

Réduire `trader/runtime/daemon.py` sans changer les flags queue canoniques :
extraire la préparation du payload atomique `plan_to_upsert` / `symbol_to_close`
vers un service applicatif pur.

## Tranche

- Ajouter `trader/application/execute_queue_plan.py`.
- Garder le daemon responsable du contexte runtime concret
  (`price`, `runtime_interval`, fraîcheur, session, daily-as-of).
- Garder `execute_queue_dispatch.py` responsable de l'enqueue/poll/fill/fail-closed.
- Couvrir OPEN, CLOSE, ADD et REVERSE par tests unitaires.
- Ajouter un garde de layout empêchant le retour du pré-calcul de plans dans le bloc queue du daemon.

## Hors périmètre

- Ne pas déplacer le `RiskGate`.
- Ne pas modifier le broker synchrone fallback.
- Ne pas retirer les flags `CASYS_QUEUE_*`.
