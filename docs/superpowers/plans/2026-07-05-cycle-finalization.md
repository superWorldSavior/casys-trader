# Plan — cycle finalization

## Objectif

Réduire la fin de `trader/runtime/daemon.py` en sortant les side effects
latéraux de fin de cycle vers un adaptateur runtime testable.

## Tranche

- Ajouter `trader/runtime/cycle_finalization.py`.
- Garder la frontière dans `runtime/`, pas `application/`, car le module compose
  des effets de bord : écriture du report, events, probes observabilité et cache
  mémoire de cycle.
- Déplacer la consolidation learnings, la collecte macro best-effort, le cache
  des rejets gross, la sonde `shadow_queue` et le `state_compare`.
- Conserver les flags queue/state actifs et inchangés :
  `CASYS_SHADOW_QUEUE_ENABLED` et `CASYS_STATE_BACKEND=sqlite` ne changent que de
  point de branchement.
- Utiliser des `Protocol` locaux pour les dépendances injectées, afin de garder
  le module testable sans dossier `ports/` générique.
- Documenter la frontière dans `docs/architecture.md` et l'index de couverture.

## Hors périmètre

- Ne pas déplacer le `RiskGate`, le broker ou l'exécution des ordres.
- Ne pas changer le mode queue decide/execute.
- Ne pas changer la politique de consolidation ou de collecte macro.
