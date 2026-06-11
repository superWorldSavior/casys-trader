# Faire entrer plus de trades — diagnostic & propositions

*2026-06-11. Analyse data sur le ledger réel (768 « missed », 6 jours, 25 symboles), cartographie du code décisionnel, revue de littérature. Tout le backtest est en pur calcul (yfinance + arithmétique), aucun appel au LLM de trading.*

## 0. Point de départ

Le bot répond HOLD avec confiance 0,9+ quasi systématiquement et ne prend presque aucun trade.
On a d'abord corrigé la métrique d'audit : un HOLD pendant un move n'est plus compté « bad » mais
« missed » (`decision_audit.py`). Résultat sur tout le ledger : **bad réel = 1,05 %**, **missed = 47,5 %**,
nonbad = 99 %. Le bot ne se trompe quasiment jamais *quand il prend position* — il s'abstient, point.

La question devient : **les `missed` sont-ils capturables ?**

## 1. Le potentiel est énorme, le réalisable par signaux purs est faible

Backtest sur les 768 missed (entrée à T, horizon 2h, stop 1 %, **coût A/R 0,10 % inclus**) :

| Stratégie | Trades | Win% | P&L/trade | P&L total |
|---|---|---|---|---|
| **ORACLE — sens parfait (borne haute)** | 224 | **79,9 %** | **+0,596 %** | **+133,6 %** |
| momentum > 0,3 % | 123 | 50,4 % | +0,003 % | +0,3 % |
| volume > 2× | 53 | 50,9 % | +0,059 % | +3,1 % |
| **ATR expansion > 1,5×** | 55 | 54,5 % | +0,271 % | +14,9 % |
| régime fort cross-asset (≥70 % aligné) | 78 | 60,3 % | +0,157 % | +12,3 % |
| **combo (ATR exp OU régime fort)** | 101 | 59,4 % | +0,184 % | +18,6 % |
| **combo confluence (ATR exp ET régime d'accord)** | 8 | **75,0 %** | **+1,116 %** | +8,9 % |

**Lecture :** si on connaissait le *sens*, c'est +133 % en 6 jours (80 % win). Les signaux mono-dimensionnels
n'en captent qu'une miette (+15 % au mieux). **L'écart oracle → meilleur signal = la valeur que peut apporter
le LLM : trancher le sens.** La confluence (ATR + régime d'accord) atteint 75 % win / +1,1 % par trade,
mais sur 8 trades seulement — direction prometteuse, échantillon minuscule.

## 2. Les moves ratés sont des régimes macro, pas des accidents isolés

- **95 % des missed arrivent en cluster** (≥3 symboles dans la même fenêtre 30 min).
- **84 % de cohérence directionnelle** intra-cluster → risk-on/off lisible sur tout l'univers.
- Ex. 08/06 09:00 UTC : actions/indices/EU/TW ↑ ensemble, pétrole ↓. 07/06 03:00–04:00 : tout ↑ sauf or/gaz.

Un signal mono-symbole est aveugle à ça. **Le cockpit injecté à l'agent contient déjà TOUS les symboles**
(`daemon.py:1164-1185`) — l'agent *voit* le tableau d'ensemble mais ne l'exploite pas comme biais de régime.

- **85 % des missed persistent** dans le même sens jusqu'à 4h (vrais trends, pas des spikes ; reversal = 4,4 %).
- **MAIS ~80 % n'ont aucun signal technique préalable** détectable au moment d'entrer → arrivent « sans prévenir »
  (news/macro). C'est cohérent avec la littérature : la majorité des moves intraday sont déclenchés par
  l'information, pas par la structure de prix.

## 3. Pourquoi le bot rate — diagnostic en 3 couches (cartographie du code)

1. **Boucle de learnings auto-renforçante.** `state/learnings.jsonl` : 49 des 50 dernières entrées sont des
   HOLD qui empilent des interdictions (« pas d'entrée sans confirmation HTF », « z extrême seul ne suffit pas »).
   Relues à chaque réveil (`daemon.py:1181-1184`). L'agent lit « reste HOLD en range » → constate range → écrit
   « confirmer le filtre » → recommence. Aucun contre-signal.
2. **Le mandat conditionne à la prudence.** `mandate/memory.md:63-66` : « si tu n'es pas convaincu, n'émets pas
   un ordre pour voir, il sera rejeté ». `mandate.md:43` : « rester HOLD autant qu'il veut ». Combiné aux
   conditions multi-critères conjointes (`|z|≥2 ET HTF aligné ET breakout`), la confidence tombe < 0,7 sur quasi
   tout.
3. **Inputs pauvres.** L'agent reçoit un cockpit compact (z, er, ac, reg, htf, aligned, sig, score volume `vs`).
   **Pas de news, pas de volume absolu/RVOL, pas d'ATR expansion, pas de biais de régime calculé.** Or ce sont
   précisément les briques qui ont un edge (§1). L'agent ne peut pas juger ce qu'il ne voit pas.

Le gate de risque lui-même n'est **pas** le bottleneck (limites larges sur 100k) : `min_trade_confidence=0.7`
→ 0,9 sans stop (`risk.py:38-39`). C'est la confidence *produite* par l'agent qui bloque.

**Important : les missed sont des décisions HOLD** — l'agent était réveillé, avait le contexte, a jugé HOLD.
C'est un problème de **jugement et d'inputs**, pas de réveil.

## 4. La tension à ne pas ignorer

« Faire entrer plus de trades » en débridant l'agent (purger learnings, baisser confidence) le fera trader plus —
mais §1 montre que **sans meilleur signal de sens, le résultat est ~50/50** (momentum = breakeven). On risque
d'échanger 47 % de *missed* (inoffensifs) contre des *bad* réels (pertes). **Débrider n'a de valeur que couplé à
de meilleurs inputs de sens.** Sinon on transforme une abstention prudente en overtrading perdant.

## 5. Propositions, classées

### A. Débrider l'agent existant (quick wins — à faire AVEC B, pas seul)
- **A1.** Purger / fenêtrer `state/learnings.jsonl` + `learnings_consolidated.json` et empêcher l'empilement
  d'interdictions (ne garder que des learnings actionnables, équilibrer entrées/abstentions).
- **A2.** Réécrire `mandate/memory.md:63-66` : retirer le conditionnement « pour voir = rejeté », garder la gestion
  du risque neutre.
- **A3.** Baisser `min_trade_confidence` 0,7 → 0,6 (`config/risk.yaml`). Seuil effectif 0,60–0,80 au lieu de 0,70–0,90.
- ⚠️ Ne déployer qu'avec un garde-fou d'overtrading et une mesure A/B du bad% réel.

### B. Donner à l'agent les inputs qui ont un edge (cœur du sujet)
- **B1. Biais de régime cross-asset calculé par le code** et injecté au prompt : « N/M de l'univers en hausse sur
  45 min → régime risk-on/off, force X% ». Mes données : suivre ce biais quand ≥70 % aligné = 60 % win.
  L'agent voit déjà les symboles mais pas la synthèse — le code la calcule, l'agent tranche.
- **B2. ATR expansion + RVOL** par symbole dans le cockpit (meilleures signatures mono : ATR exp +0,27 %/trade).
- **B3.** Faire de l'agent un **juge de confluence** : signaux faibles (ATR/RVOL/régime) en INPUT, l'agent valide
  le sens. La confluence ATR+régime donne 75 % win dans le backtest — c'est exactement un job de LLM-judge.

### C. Adresser les 80 % news-driven (plus gros effort, plus gros gisement)
- **C1. Flux news temps réel** (Polygon/Benzinga ~$30–99/mois) + LLM qui qualifie *surprise vs consensus* et
  direction. C'est là que le LLM bat le NLP classique (comprend la surprise, le contexte sectoriel).
- **C2. Calendrier économique** (gratuit) : ne pas trader 5 min pré-event, entrer sur cassure confirmée post-event
  si le LLM juge la surprise significative.
- **C3.** Détection d'anomalie RVOL → dispatch LLM (web/RAG) pour trouver la news → décision contextuelle.

## 6. Ce que je recommande de tester en premier
1. **B1 + B3** (régime cross-asset calculé + agent juge de confluence) : zéro coût externe, exploite la donnée
   qu'on a déjà, et c'est le différenciateur le plus pur. Mesurable en backtest avant tout déploiement.
2. **A1/A2** en parallèle (casser la boucle de learnings) — mais avec garde-fou et mesure.
3. **C1** ensuite si on veut attaquer le gros du gisement (les 80 %).

## 7. Caveats méthodo (à ne pas oublier)
- **6 jours, un seul régime de marché.** Aucune validation out-of-sample. Les +X % sont indicatifs, pas un edge prouvé.
- Paper, MAE sur barres 15 min, coûts estimés (0,10 % A/R) — à affiner par actif.
- L'oracle suppose un sens parfait : c'est une **borne haute théorique**, pas atteignable.
- LLM-as-judge intraday : prometteur en théorie, **non prouvé empiriquement** dans la littérature à cette échelle.
- Risques LLM connus : look-ahead bias, hallucination sur chiffres, narrative bias → demander des jugements
  qualitatifs (GO/SKIP/UNCERTAIN + conviction), pas des prédictions chiffrées.
