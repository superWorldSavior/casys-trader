# Plan — queue runtime bootstrap

## Objectif

Réduire `trader/runtime/daemon.py::main()` en sortant le bootstrap concret des
pools de tâches `decide` et `execute_order` vers un adaptateur runtime dédié.

## Tranche

- Ajouter `trader/runtime/queue_runtime.py`.
- Conserver les flags actifs et équivalents :
  `CASYS_QUEUE_DECIDE_ENABLED`, `CASYS_QUEUE_EXECUTE_ENABLED`,
  `CASYS_STATE_BACKEND=sqlite`.
- Garder `run_cycle()` inchangé sur son contrat : il reçoit toujours
  `queue_decide_enabled`, `task_ledger`, `queue_execute_enabled` et
  `execute_ledger`.
- Garder les handlers métier dans `application/` et les backends dans
  `infrastructure/`; `queue_runtime` ne fait que composer les objets au boot.
- Utiliser des factories injectables et des `Protocol` locaux plutôt qu'un
  dossier générique `ports/`.
- Ajouter un garde de layout empêchant le retour des imports queue/SQLite
  concrets dans `daemon.py`.

## Hors périmètre

- Ne pas changer la sémantique des files ni leur persistence.
- Ne pas modifier le polling ou le fail-closed `execute_order`.
- Ne pas déplacer le shutdown des pools dans cette tranche.
