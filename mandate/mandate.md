# Mandat de l'agent

> Édité en boucle 1 (Erwan + Claude). L'agent runtime lit ce fichier à chaque
> réveil. C'est ICI qu'on définit objectif, marchés, contraintes — PAS dans le code.

## Objectif

_(à définir avec l'agent — ex. croissance du capital sur l'univers donné, horizon
court terme, tolérance au risque…)_

## Marchés autorisés

Voir `config/universe.yaml`. v1 : ETF + actions listés US.

## Contraintes

- Paper trading uniquement pour l'instant.
- Le risk gate (`config/risk.yaml`) est une borne dure non négociable.
- _(autres contraintes à préciser : pas de short ? horizon de détention ? secteurs exclus ?)_

## KPI suivis

_(à définir : rendement total, drawdown max, hit rate, … — l'agent ajuste sa
stratégie en fonction)_
