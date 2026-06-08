# Mémoire de l'agent

> L'agent runtime écrit ici sa stratégie évolutive et ses learnings (via
> `tools/memory.py::append_learning`). Éditable aussi à la main en boucle 1.
> La stratégie ci-dessous est un POINT DE DÉPART — l'agent l'affine selon ses KPI.

## Stratégie v1 — exploite ton edge d'IA (PAS le kit retail)

> Tu n'es pas un humain devant un chart. Oublie le combo EMA/VWAP/RSI que tout le
> monde utilise — c'est sur-exploité et sans bord. Ton avantage : tu reçois un
> cockpit cross-asset compact sur tout l'univers, et tu peux demander au daemon
> des indicateurs déterministes ciblés quand un signal mérite d'être creusé.
>
> Profil : **scalping gentil / momentum intraday court** sur barres **horaires**.
> Entrées sélectives, sorties rapides, HOLD si pas d'edge net.

### Edge n°1 — Analyse cross-asset (ton vrai différentiel)
À chaque décision tu reçois `context.cockpit` pour TOUT l'univers. Il est compact
mais suffisant pour scanner les relations :
- **Familles corrélées** : indices (SPY/QQQ/DIA), commodities futures
  (CL=F/BZ=F/NG=F), forex majors, pays (EWQ/EWT), défense (ITA), single names
  (NVDA/AAPL).
- **Lead-lag** : un membre bouge avant les autres (CL=F/BZ=F avant XLE ; QQQ
  avant NVDA ; USDJPY/EURJPY pour le stress yen).
  Si le leader bouge et le suiveur n'a pas encore suivi → edge directionnel.
- **Non-confirmation** : QQQ monte mais NVDA ne suit pas → méfiance, voire fade.
- **Force relative cross-sectionnelle** : dans une famille, long le plus fort /
  short le plus faible.
- **Spreads mean-reverting** entre proches (SPY/DIA, EWQ/EWT) : si l'écart
  s'éloigne anormalement de sa norme récente, parie sur le retour.

### Edge n°2 — Identifie le RÉGIME avant de choisir la tactique
N'applique jamais une règle en aveugle. Estime d'abord le régime du symbole avec
les colonnes compactes du cockpit, puis demande `REQUEST_CONTEXT` seulement si
un complément est vraiment utile :
- **Kaufman Efficiency Ratio** (déplacement net / somme des |variations|) ou
  **exposant de Hurst** : efficience élevée / H>0.5 → marché qui **tend** (joue le
  momentum) ; efficience faible / H<0.5 → marché en **range/chop** (joue la
  mean-reversion, ou s'abstient).
- **Autocorrélation des returns** à lag court : positive → momentum ; négative →
  mean-reversion.
→ Momentum SEULEMENT en régime trending ; mean-reversion en range ; sinon HOLD.

### Edge n°3 — Stats robustes pour entrée / sortie / sizing
- **Mean-reversion** : z-score du prix vs sa distribution roulante ; entre quand
  |z| est extrême ET le régime est ranging.
- **Volatilité via estimateurs OHLC** (Yang-Zhang, Garman-Klass) plutôt qu'un ATR
  close-only : plus efficace, utilise high/low/open. Sert à dimensionner et à
  placer le stop.
- **Stop & take-profit en unités de volatilité** (pas un % fixe) : vise un ratio
  risque/rendement ~**1:2** exprimé en multiples de vol.

### Garde-fous de scalping (discipline)
- **Petites tailles** : ~1% du capital risqué par trade. `quantity` modeste.
- Préfère les **heures liquides** (≈90 min après l'ouverture US, dernière heure).
- **Coupe vite** si ça ne va pas dans ton sens ; ne t'accroche pas à un scalp mort.
- Le **risk gate** reste un fusible dur au-dessus de tout ça.

### Données disponibles & limites (sois lucide)
- Le contexte initial ne contient PAS les barres brutes. Il contient
  `context.cockpit` : `cols` + `rows`, avec colonnes compactes `r`, `vol`, `z`,
  `er`, `ac`, `rs`, `sz`. Si tu as besoin d'un calcul ciblé, demande
  `REQUEST_CONTEXT` sur quelques symboles/indicateurs, pas plus.
- Le cockpit fournit aussi des **labels de régime pré-mâchés** par le code :
  `reg` (`trending_up`/`trending_down`/`range`/`breakout`), `vs` (vol
  `low`/`normal`/`high`), `st` (`stretched`), `cndle` (bougie). Sers-t'en pour
  l'Edge n°2 (identifier le régime) au lieu de le recalculer de tête.
- PAS de carnet d'ordres / order-flow → pas de VPIN tick-level fiable. Ton bord
  est le **cross-asset + le régime statistique**, pas la microstructure fine.
- `context["symbol"]` = le symbole à décider ce tour ; les autres servent de
  contexte cross-asset.

## Learnings

_(vide — l'agent remplit au fil de l'eau)_
