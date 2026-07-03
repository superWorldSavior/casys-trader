# Phase 6 (sketch) — granularité des action tools : rendre à l'agent son génie financier

> **Type** : Design sketch (pas encore un design validé).
> **Parent** : `2026-06-29-agent-domain-tools-design.md` (Phases 1-5 livrées).
> **Déclencheur** : une fois les action tools par symbole en place (contrat
> `calls:[...]`, commit 7ceca4a + durcissements 9bbfa6a/d11c559/4b3b548), on
> constate que le vocabulaire reste **grossier** sur plusieurs axes. Ce sketch
> liste les leviers qui donneraient à l'agent une vraie latitude de gestion.

## Principe directeur (AX)

> L'agent doit raisonner en **thèse** et en **risque**, jamais en plomberie.

Chaque endroit où l'agent doit calculer ce que le daemon connaît déjà — la side
d'après le sens de la position, la quantité d'après le capital / le prix / le FX,
la distance de stop en unités — est une **fuite de granularité** : ça consomme du
raisonnement sur de l'arithmétique, et ça multiplie les façons de tomber en HOLD
technique (cf. `order_side_required`). La règle de conception de la Phase 6 :
*offrir l'intention de haut niveau, laisser le daemon résoudre le déterministe.*

## Les leviers manquants (classés par valeur / effort)

### L1 — Sizing en risque, pas en quantité *(valeur : haute · effort : moyen)*
Aujourd'hui `propose_order` prend `qty` (unités). L'agent doit convertir capital →
unités en tenant compte du prix natif et du FX (cf. chantier FX base USD). Or le
daemon a déjà `max_quantity_at_risk` (`trader/execution/risk.py:98`) et connaît le
capital. **Proposer** : `propose_order{risk_pct: 0.5}` ou `{risk_r: 1.0}` (risque
par trade en % de l'equity ou en R depuis la distance de stop). Le daemon
dimensionne. L'agent exprime *combien il est prêt à perdre sur cette thèse*, pas un
nombre d'actions. C'est le levier le plus AX du lot.

### L2 — CLOSE / REDUCE / REVERSE position-aware *(valeur : haute · effort : faible)*
Dette déjà identifiée (fact-check F2). Aujourd'hui ces intents exigent `side` +
`qty` explicites, sinon HOLD tracé. **Proposer** : `{intent: CLOSE}` ferme toute la
position ; `{intent: REDUCE, fraction: 0.5}` réduit de moitié ; `{intent: REVERSE}`
inverse — side et qty dérivés du sens/taille de la position au portefeuille. Le
daemon a déjà `reverse_open_quantity` (`order_admission.py`) : la brique existe.

### L3 — Amender un plan / une position vivante *(valeur : haute · effort : moyen)*
L'agent peut poser un plan de sortie à l'ouverture, mais pas **ajuster** une
position déjà ouverte sans la fermer/rouvrir. Il ne peut pas remonter un stop au
break-even, déplacer un TP, resserrer un trailing en cours de route — la gestion
active du risque, cœur du métier. **Proposer** : `amend_exit{hard_stop?,
take_profits?, trailing?, protect?}` qui patche le `TradePlan` ouvert du symbole
(réutilise `TradePlanStore.upsert`). Scale-out manuel, verrouillage de gains,
trailing discrétionnaire deviennent exprimables.

### L4 — Scale-in / pyramiding explicite *(valeur : moyenne · effort : moyen)*
Ajouter à une position gagnante est aujourd'hui ambigu (un OPEN sur position
existante devient REDUCE/REVERSE selon la side). **Proposer** un intent `ADD`
(même side que la position) avec son propre sizing (L1) et sa propre logique de
stop combiné. Permet la construction progressive d'une conviction.

### L5 — Réveils et veilles événementiels *(valeur : moyenne · effort : moyen)*
`set_next_wake` est un timer en minutes ; `propose_indicator_watch` déclenche sur
indicateurs. Manque le déclenchement sur **événement calendaire** : ouverture de
séance, earnings, publication macro (FOMC/CPI). Lien direct avec le chantier
macro/news (collecte `macro_next` déjà en place). **Proposer** :
`set_next_wake{on: session_open | pre_earnings | macro_event}` — l'agent se
reprogramme sur ce qui compte, pas sur une horloge aveugle.
**remarque erwan**
Ok c est top mais un timer en minute c est bien aussi, faut juste enrichir le set nect wake. d ailleurs : c est fait exprés d avoir un autre outil 'propose indicator watch' alors qu on a aussi set next wake ? Faut pas confondre avec les plans armés, mais a mon sens on devrait avoir un tool explicite pour le next wake et differentes options non

### L6 — Attribution structurée à la décision *(valeur : moyenne · effort : faible)*
`record_learning` est du texte libre. **Proposer** un canal structuré léger sur
`propose_order` : `thesis{setup, horizon, invalidation}` — tag machine-lisible qui
alimente l'attribution et le RAG learnings (corrélation setup → résultat réel) au
lieu d'un post-mortem en prose. Améliore la boucle d'apprentissage sans coût pour
l'agent.

### L7 — OCO / TP conditionnels explicites *(valeur : moyenne · effort : élevé)*
Reprend le chantier OCO (conscience d'état) : lier explicitement des ordres
(un TP annule le stop et vice-versa), poser des TP conditionnés à un indicateur.
Évite les whipsaws de plans armés en aveugle. À cadrer avec ce chantier existant.

## Ce qui n'est PAS un manque de granularité (à ne pas confondre)
- L'**exit_plan** est déjà riche (hard_stop relatif, TP en R, trailing typé,
  profit_protection arm/giveback/lock). Ne pas sur-ajouter ici.
- Les **outils read-only** (pull) couvrent déjà position/risque/plans/attribution/
  mémoire. Le manque n'est pas côté observation mais côté **action**.

## Dettes techniques (issues du fact-check 2026-07-03)
- ~~Réécriture fine des outcomes bruts de `runtime.tool_calls`~~ — **SOLDÉE
  2026-07-03** : `tool_trace.finalize_action_tool_outcomes` réécrit, dans le
  `DecisionRecorder`, le résultat RÉEL des 5 action tools finaux (executed/blocked,
  applied/clamped, created/rejected, cancelled/rejected). Les outils de la tournée
  read-only (`recall_learnings`/`get_*`) sont préservés : leur `outcome == "ok"`
  pilote le recall (`decision_recorder`/`planner_batch`), et les noms diffèrent
  (`record_learning` ≠ `recall_learnings`), donc la discrimination par nom est sûre.

## Prochaine étape
Prioriser L1 + L2 (haute valeur, effort maîtrisé, position-aware = dette connue)
pour un vrai design Phase 6. Les autres leviers en backlog, à réordonner selon les
observations forward une fois les action tools mesurés en live.
