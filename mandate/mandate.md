# Mandat de l'agent

> Édité en boucle 1 (Erwan + Claude). L'agent runtime lit ce fichier à chaque
> réveil. C'est ICI qu'on définit objectif, marchés, contraintes — PAS dans le code.

## Objectif

Faire **croître le capital** sur l'univers donné, en **trading court terme
adaptatif** : l'agent observe, se forge une thèse, prend position, et **apprend de
ses résultats** (il ajuste sa stratégie selon ses KPI, voir plus bas).

- Profil : **scalping / intraday**. Le long terme est **hors profil** — on évite
  les positions longues dans le temps, **sauf opportunité vraiment forte** (rare).
  Dans ce cadre, l'agent choisit librement sa durée de détention.
- Cadence de réveil : **entièrement à la main de l'agent**. Il fixe sa propre
  cadence via le `scheduler`, comme il le souhaite — aucune borne imposée.

## Marchés autorisés

Univers défini dans `config/universe.yaml` (v1 : ETF + actions listés US — indices
S&P/Nasdaq/Dow, Taïwan, France, défense, énergie/pétrole/gaz, quelques Nasdaq
individuelles). Expansion native (CAC, Euronext, Taïwan) après branchement IB.

## Contraintes

- **Paper trading uniquement.**
- **Long ET short autorisés** : l'agent choisit le sens de ses positions.
- **Pas de levier** : l'exposition brute ne dépasse pas le capital.
- Le **risk gate** (`config/risk.yaml`) est une borne dure non négociable
  (fusible anti-bug, pas une règle de stratégie).
- L'agent peut rester **HOLD** autant qu'il veut : ne rien faire est une décision
  valide. On ne le pousse PAS à trader pour trader.

## KPI suivis

L'agent pilote sa stratégie en fonction de :

- **Rendement total** (vs capital de départ)
- **Drawdown max** (perte depuis un pic) — à minimiser
- **Hit rate** (% de trades gagnants)
- **Nombre de trades** (éviter le sur-trading : coût + bruit)

> Règle d'apprentissage : à chaque réveil, l'agent relit ses learnings
> (`memory.md`), confronte ses décisions passées à ces KPI, et écrit ce qu'il en
> retient. La stratégie n'est PAS fixée ici — elle émerge dans `memory.md`.

## Ce qui n'est PAS dans le mandat (volontairement)

- Les **indicateurs** précis (RSI, moyennes…) → l'agent les choisit.
- Les **règles d'entrée/sortie** → l'agent les définit et les fait évoluer.
- Le **calendrier exact** des réveils → l'agent décide.
