# Plan — runtime shutdown

## Objectif

Réduire `trader/runtime/daemon.py::main()` en sortant le shutdown best-effort
des ressources runtime vers un adaptateur dédié.

## Tranche

- Ajouter `trader/runtime/runtime_shutdown.py`.
- Centraliser l'arrêt best-effort des pools queue decide/execute.
- Centraliser le disconnect de la source de données.
- Centraliser la libération sûre du pid file via `release_pid_file`.
- Remplacer le bloc `finally` inline de `daemon.main()`.
- Ajouter un garde de layout contre le retour de `.stop()` / `release_pid_file()`
  directs dans `main()`.

## Hors périmètre

- Ne pas changer la sémantique de `release_pid_file`.
- Ne pas modifier la logique de claim du pid file au boot.
- Ne pas changer les pools queue ni leur politique d'arrêt.
