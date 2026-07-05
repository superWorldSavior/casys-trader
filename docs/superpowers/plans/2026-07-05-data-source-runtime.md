# Plan — data-source runtime

## Objectif

Réduire `trader/runtime/daemon.py::main()` en sortant le bootstrap concret des
sources de données vers un adaptateur runtime dédié.

## Tranche

- Ajouter `trader/runtime/data_source_runtime.py`.
- Valider la config `config/data_sources.yaml` une seule fois avant la boucle,
  avec injection de `parse_data_sources_config` pour préserver les tests daemon.
- Composer les modes direct IB et composite yfinance/IB dans le module runtime.
- Garder les règles existantes :
  - profil `prod` : IB obligatoire ;
  - profil `paper` : IB optionnel avec fallback yfinance ;
  - IB direct sans config : `market_data_type=3`.
- Déplacer le lazy attach IB et le détachement IB sur erreur de connexion hors de
  `daemon.main()`.
- Garder les classes et fonctions concrètes injectables depuis le daemon afin de
  préserver les monkeypatchs historiques.
- Ajouter un garde de layout empêchant le retour des appels directs
  `connect_ib`, `IBDataSource`, `CompositeDataSource` et `parse_data_sources_config`
  dans `daemon.py`.

## Hors périmètre

- Ne pas modifier le contrat `run_cycle()`.
- Ne pas changer les profils ni routes de `config/data_sources.yaml`.
- Ne pas déplacer les adapters marché eux-mêmes hors de `trader/market/`.
