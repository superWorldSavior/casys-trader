# Plan — market rotation runtime

## Objectif

Réduire `trader/runtime/daemon.py::main()` en sortant la glue du tick D10
rotation/radar vers un adaptateur runtime dédié.

## Tranche

- Ajouter `trader/runtime/market_rotation_runtime.py`.
- Garder le tick rotation best-effort : une erreur rotation ne doit jamais faire
  tomber le daemon.
- Charger `radar.yaml`, construire l'override LLM uniquement si le paramètre est
  activé, lire le cache `state/last_regime.json`, puis appeler
  `market.rotation.venues.tick()`.
- Garder les imports concrets rotation/radar hors de `daemon.main()`.
- Préserver le contrat du cycle : `daemon.main()` fournit seulement `ROOT/config`,
  `STATE_DIR`, `loop_now` et le logger.
- Ajouter une couture daemon et un garde de layout pour éviter que les imports
  inline reviennent dans `daemon.py`.

## Hors périmètre

- Ne pas modifier la politique de rotation d'univers.
- Ne pas changer le format de `last_regime.json` ni le contenu de `radar.yaml`.
- Ne pas déplacer les modules métier de `trader/market/rotation/`.
