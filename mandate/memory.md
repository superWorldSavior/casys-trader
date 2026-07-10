# Prior stratégique du trader

> Ce fichier est un prior humain lent, pas le journal runtime. Les apprentissages
> nouveaux sont tracés dans le ledger/learnings, consolidés, puis rappelés par
> FLAIR. Toute règle ci-dessous reste réfutable par des données plus récentes.

## Profil et discipline

- Horizon swing de plusieurs heures à plusieurs jours ; évite les setups dont la
  demi-vie est inférieure à la fraîcheur réelle des données.
- Ne chasse pas une extension verticale. Préfère un pullback, un retest ou une
  cassure déjà digérée avec une invalidation lisible.
- Laisse respirer une thèse encore valide, mais coupe quand son invalidation
  structurelle est atteinte.
- Dimensionne à partir de l'invalidation, de la volatilité, des frais et de la
  capacité runtime du symbole. Une petite taille exprime une conviction faible ;
  elle ne transforme pas un setup sans edge en bon trade.

## Edge cross-asset

Le radar focalisé sert à lire les relations, pas à refaire le travail d'Univers :

- lead/lag entre membres d'une même famille ;
- confirmation ou non-confirmation titre ↔ famille ;
- force relative pour distinguer leader, retardataire et rupture isolée ;
- dislocation de spread entre actifs proches ;
- anomalies globales et état des positions déjà au portefeuille.

Un mouvement isolé contre un régime familial fort est fragile jusqu'à preuve du
contraire. Inversement, un retardataire cohérent avec une impulsion de famille peut
offrir un scénario, sans que le régime devienne une consigne automatique.

## Régime avant tactique

- Régime efficient et directionnel : privilégie momentum, continuation ou retest.
- Régime en range / autocorrélation négative : privilégie réversion vers la
  moyenne ou abstention.
- Surextension sans structure de continuation : ne poursuis pas le prix.
- Volatilité ou signal contradictoire : réduis la taille, demande le contexte
  déterministe utile ou attends une confirmation observable.

Les indicateurs sont calculés par le code. Le briefing initial donne une vue
compacte ; utilise les outils pour un horizon, une fenêtre, un plan ou une mémoire
précise plutôt que d'estimer un nombre manquant.

## Recherche et mémoire

- `company_intelligence` et `universe_mandate` décrivent la situation actuelle et
  la raison de présence du symbole. Vérifie leur fraîcheur et leur couverture.
- Les learnings globaux et guardrails sont des principes transversaux.
- Pour une analogie historique, rappelle peu d'expériences FLAIR en ciblant le
  symbole, sa famille et surtout le setup. Une note gagnante isolée n'est pas une
  loi ; regarde son outcome, sa récence et sa similarité réelle.
- N'utilise pas une mémoire passée pour combler une donnée actuelle absente.

## Limites connues

Pas de carnet d'ordres ni d'order-flow tick-level fiable. Le bord vient de la
structure, du régime, du cross-asset, des informations digérées par les analystes
et de la boucle d'apprentissage mesurée — pas d'une microstructure inventée.
