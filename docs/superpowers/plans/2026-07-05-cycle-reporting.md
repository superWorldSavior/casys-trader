# Plan — cycle reporting

## Objectif

Réduire `trader/runtime/daemon.py::main()` en sortant la persistance
`last_report.json` + `history.jsonl` vers un adaptateur runtime dédié.

## Tranche

- Ajouter `trader/runtime/cycle_reporting.py`.
- Centraliser la règle "cycle actif sans symbole dû" :
  décisions, sorties planifiées ou exit-watch triggers.
- Ajouter `RuntimeStateWriter.write_last_report()`.
- Remplacer les writes inline de `daemon.main()` par
  `cycle_reporting.persist_cycle_report(...)`.
- Ajouter un garde de layout empêchant le retour du chemin
  `last_report.json` en dur dans `main()`.

## Hors périmètre

- Ne pas modifier le contenu des rapports.
- Ne pas changer `current_report.json`, écrit pendant `run_cycle()`.
- Ne pas déplacer la politique de sleep ou de sélection des symboles dus.
