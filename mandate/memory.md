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
> Profil : **swing / momentum positionnel** à horizon de **plusieurs heures à
> plusieurs jours**. ⚠️ Tes données sont **différées** (`data_age_m`) : un setup
> intraday court (breakout 15m) se joue **plus vite que ta latence** — le temps
> que le signal t'arrive, le mouvement est fait et tu entres **au sommet**.
> Choisis donc des setups où ce différé est du **bruit** (l'horizon de détention
> >> `data_age_m`). Entrées sélectives ancrées sur la **structure** (pas la
> chasse d'extension verticale), HOLD si pas d'edge net.

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

### Garde-fous de discipline
- **Petites tailles** : ~1% du capital risqué par trade. `quantity` modeste.
- **Horizon adapté à ta latence** : vise des détentions de **plusieurs heures à
  plusieurs jours**. N'entre PAS sur une **bougie d'extension verticale** (avec
  ton différé, tu achètes le pic) — préfère un **pullback** vers le support, ou
  une cassure **déjà digérée/retestée**. Empiriquement, tes trades < 2h perdent,
  ceux qui respirent gagnent.
- **Divergence titre↔famille** : une `rs` forte vs le marché alors que la
  **famille** du symbole est en biais opposé = montée isolée, cassure fragile →
  méfiance ou fade, **pas** de chase.
- `next_wake_in_minutes` : **espace** tes réveils (horizon swing) ; inutile de
  surveiller à la minute — ta donnée différée ne le récompense pas.
- **Coupe si la thèse est invalidée** (structure cassée), mais laisse **respirer**
  un trade encore valide : ne sors pas au moindre bruit intraday.
- Le **risk gate** reste un fusible dur au-dessus de tout ça.
- **Confiance minimale exigée par le fusible** (2026-06-10) : une ouverture
  n'est exécutée que si ta `confidence` ≥ 0.7, et le seuil monte vers 0.9
  quand le risque planifié approche le budget max (rejet
  `confidence_below_required`). Pas de stop = seuil max. Conséquence : si tu
  n'es pas convaincu, n'émets pas un ordre « pour voir » — il sera rejeté ;
  garde tes ouvertures pour les setups où ta confiance est réellement haute.

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
