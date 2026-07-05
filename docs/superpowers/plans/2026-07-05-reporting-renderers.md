# Plan - reporting renderers

## Objectif

Separer les projections reporting et leur rendu texte, sans casser les anciens
imports `reporting.stats` et `reporting.tool_usage`.

## Tranche

- Ajouter `trader/reporting/renderers/live_kpis.py`.
- Ajouter `trader/reporting/renderers/tool_usage.py`.
- Garder `trader/reporting/stats.py` et `trader/reporting/tool_usage.py`
  comme facades publiques.
- Brancher les CLI sur projection canonique + renderer canonique.
- Ajouter un garde layout prouvant que les facades reexportent les renderers.

## Hors perimetre

- Ne pas changer les payloads de KPI ni de rapport tool usage.
- Ne pas modifier les commandes CLI ni leurs options.
- Ne pas toucher aux projections `read_models`.
