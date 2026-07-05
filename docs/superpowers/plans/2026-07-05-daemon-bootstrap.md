# Plan — daemon bootstrap

## Objectif

Réduire `trader/runtime/daemon.py::main()` en sortant le bootstrap runtime de
démarrage vers un adaptateur dédié.

## Tranche

- Ajouter `trader/runtime/daemon_bootstrap.py`.
- Garder l'ordre existant :
  - rotation mensuelle `decisions.jsonl` puis `events.jsonl` ;
  - bootstrap backend état (`CASYS_STATE_BACKEND`) avec cash initial ;
  - construction scheduler.
- Conserver la rotation ledger best-effort : une erreur de rotation loggue un
  warning et ne bloque pas le daemon.
- Garder `daemon.main()` propriétaire des choix env/CLI et de l'identité process
  (`daemon.pid`), mais déléguer les side effects state/ledger/scheduler.
- Injecter `bootstrap_state_backend` et `make_scheduler` depuis `daemon.py` afin
  de préserver les tests runtime historiques.
- Ajouter une couture daemon et un garde de layout empêchant le retour des appels
  directs `rotate_monthly`, `bootstrap_state_backend`, `make_scheduler` et
  `load_starting_cash` dans `daemon.py`.

## Hors périmètre

- Ne pas déplacer la logique `run_cycle()`.
- Ne pas changer le format des ledgers, archives, shadows JSON ou stores SQLite.
- Ne pas modifier la stratégie PID file / shutdown.
