# Plan — cycle dispatch

## Objectif

Réduire `trader/runtime/daemon.py::main()` en sortant le paquet d'appel
`run_cycle()` vers un adaptateur runtime dédié.

## Tranche

- Ajouter `trader/runtime/cycle_dispatch.py`.
- Centraliser le contrat d'appel runtime dans `RunCycleRuntimeContext`.
- Remplacer les deux appels `run_cycle(...)` dupliqués dans `daemon.main()` par
  `cycle_dispatch.dispatch_run_cycle(...)`.
- Conserver `daemon.main()` propriétaire du choix des symboles dus, du sleep et
  des écritures `last_report.json` / historique.
- Préserver la couture historique `daemon.run_cycle` en la passant comme
  `run_cycle_fn` au dispatcher.
- Ajouter une couture daemon et un garde de layout empêchant le retour d'appels
  directs `run_cycle(...)` dans `main()`.

## Hors périmètre

- Ne pas modifier `run_cycle()` ni sa sémantique.
- Ne pas changer la sélection des symboles dus, les veilles, ni le calcul de
  `sleep_seconds`.
- Ne pas déplacer l'écriture des rapports de cycle dans cette tranche.
