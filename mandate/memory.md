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
- Un setup cohérent n'a pas besoin de cumuler pullback, reclaim, breakout, volume
  et alignement parfait. Quand l'edge existe mais reste incertain, exprime cette
  incertitude par la taille ou par un plan armé plutôt que par un HOLD automatique.

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
- Volatilité ou signal contradictoire : réduis la taille ou demande un seul fait
  déterministe matériel. N'empile pas des confirmations qui n'étaient pas requises
  dans le scénario initial.

Les indicateurs sont calculés par le code. Le briefing initial donne une vue
compacte ; utilise les outils pour un horizon, une fenêtre, un plan ou une mémoire
précise plutôt que d'estimer un nombre manquant.

## Continuité des scénarios

- Déclare ensemble les conditions déjà nécessaires au scénario. Si le trigger est
  suffisant et l'ordre complet définissable, arme-le ; sinon pose une veille sur
  l'unique inconnue matérielle restante.
- Quand un trigger choisi est atteint, rejuge la thèse initiale : agis ou arme si
  elle reste valide. Un nouveau HOLD doit nommer un fait nouveau matériel ; une
  faiblesse déjà connue ne permet pas de déplacer le but.
- Une expiration, une donnée stale, une session fermée, une invalidation
  structurelle ou un changement matériel de news, régime, position ou capacité de
  risque autorise une nouvelle décision. Ce sont des faits, pas une hésitation.
- Pour le swing, le daily et le 4h gouvernent la direction et l'invalidation. Le
  15m règle le timing ; il ne rajoute pas une série de vetos après le trigger.

## Recherche et mémoire

- `universe_mandate` décrit le mandat et porte le micro de référence. Un
  `company_intelligence_delta` éventuel décrit seulement un brief plus récent.
  Vérifie leur fraîcheur et leur couverture.
- Les learnings globaux et guardrails sont des principes transversaux.
- Pour une analogie historique, rappelle peu d'expériences FLAIR en ciblant le
  symbole, sa famille et surtout le setup. Une note gagnante isolée n'est pas une
  loi ; regarde son outcome, sa récence et sa similarité réelle.
- N'utilise pas une mémoire passée pour combler une donnée actuelle absente.

## Limites connues

Pas de carnet d'ordres ni d'order-flow tick-level fiable. Le bord vient de la
structure, du régime, du cross-asset, des informations digérées par les analystes
et de la boucle d'apprentissage mesurée — pas d'une microstructure inventée.
