# Référence — Contexte agent (le cockpit envoyé au LLM)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent/context`, `trader/application/decide/context_projection` · **Rôle** : construit le contexte de cycle, puis sa projection focalisée + la recherche à la demande.

`build_market_cockpit` construit le dict **`cockpit`** — **une clé** du
`shared_context`. Le `shared_context` complet est assemblé dans `runtime/daemon`
(il ajoute `now`, `now_human`, `market_clocks`, `portfolio`,
`risk_limits`/`risk_capacity`, `kpis`, `attribution`, `meta_performance`,
`learnings`, `regime_families`, `semantic`, `stale_market_data`,
`active_plans_summary`). En batch legacy, ce contexte est injecté une fois par
lot. En queue grain-symbole (production),
`project_shared_context_for_symbol(...)` le réduit avant enqueue : faits calculés
par le code, pas de prose (AX).

## Projection queue `decision_focus_v1`

Le trader ne refait pas le travail d'Univers. Le prompt queue suit trois niveaux :

| Niveau | Contenu |
|---|---|
| Push obligatoire | recherche micro/news + mandat Univers du symbole, structure, fraîcheur/session, triggers, risque exact, plans locaux, portefeuille et guardrails |
| Résumé | radar borné, pairs de famille, positions, anomalies globales, régimes, KPI/attribution et compteurs de plans |
| Pull | indicateurs, plans détaillés, attribution complète et expériences FLAIR via les domain tools |

Le radar contient au plus 32 lignes compactes : cible, sept pairs au plus,
positions du portefeuille, highlights globaux, puis anomalies par score. Le bloc
`focus` conserve les colonnes complètes pour la cible et ses pairs retenus. La
capacité `risk_capacity.per_symbol` ne garde que la cible ; les agrégats gross,
equity et limites restent globaux.

Cette projection ne supprime aucune capacité analytique :
`get_indicator_context` calcule les détails absents ; `get_active_plans` lit le
snapshot complet du cycle ; `get_attribution` lit l'attribution complète conservée
hors prompt dans `WorkerCycleContextHandle` ; `recall_learnings` interroge
`learnings.db`.

## `portfolio` — cash ledger vs cash libre

`portfolio.cash`/`portfolio.cash_ledger` est le ledger broker paper. Une vente
short crédite ce ledger, comme une comptabilité de fill, mais ce montant ne doit
pas être lu comme du cash mobilisable. `portfolio.cash_available` retire
l'exposition short courante (`short_exposure_usd`) et sert à l'affichage humain.
Pour dimensionner une nouvelle ouverture, l'agent lit surtout
`risk_capacity.gross_remaining_usd` et les plafonds `max_*_qty`.

## `build_market_cockpit(...)`

Construit le `cockpit` : par symbole, prix, les 7 indicateurs numériques cœur
(`return`, `volatility`, `z_score`, `efficiency_ratio`, `autocorrelation`,
`relative_strength`, `spread_zscore`) en **15m + daily**, régime,
signaux HTF, distances swing, frais, **devise/fx/budgets** — compacté
(`_compact_price`, arrondis). ⚠️ **`data_age_m` et `session` ne viennent PAS d'ici** :
ils sont calculés par `application/decide/planner_batch._symbol_facts()` et injectés
séparément par symbole (`per_symbol_payload`).

- `_swing_distances_pct(...)` — distances aux niveaux swing en **fraction du prix**
  (0–1, ex. 5 % = `0.05` — direct compatible `min_pct`/`max_pct` des stops).
- `_family_code(value)` — code famille compact.

C'est la source du snapshot global. En queue, seule sa projection focalisée est
poussée ; si elle suffit, l'agent décide directement, sinon il pull le complément
précis (cf. [agent-tools](agent-tools.md)).

## `resolve_indicator_requests(...)`

Résout une demande de contexte (`REQUEST_CONTEXT` ou `get_indicator_context`) :
quand l'agent veut un autre horizon, une autre fenêtre ou les indicateurs absents
du cockpit compact, cette fonction les calcule et les réinjecte au passage suivant.
Par défaut, **tous les indicateurs valides demandés** sont retournés : le catalogue
gouverné (16 indicateurs) est déjà la borne naturelle. Le nombre de requêtes/tool
calls reste borné pour maîtriser les fetchs ; un éventuel cap opérateur explicite
sur les indicateurs est signalé par `indicators_truncated`, jamais silencieux.

Chaque résultat contient aussi `structure` avec la timeframe réellement demandée,
`bar_as_of`, `bars_available`, le prix, les swings exacts 24/48, `atr_pct_14` et
`relative_volume_20`. L'agent peut donc faire ses calculs de niveau/R/Fibonacci
sans reconstruire les ancres à partir de distances compactes.

## Faits par symbole

Le batch LLM reçoit aussi un payload `per_symbol` assemblé dans
`application/decide/planner_batch.py`. Ces champs ne sont visibles que par le symbole
concerné :

| Champ | Sens |
|---|---|
| `data_age_m`, `session` | fraîcheur et session du symbole |
| `active_watches` | veilles/plans armés encore actifs |
| `indicator_triggers` | conditions de watch/exit_watch qui viennent de se réaliser |
| `wake_reasons` | raisons de réveil sans condition déclenchée, notamment `watch_expired` et `armed_plan_expired` |
| `execution`, `planning` | éligibilité marché/exécution si disponible |
| `last_llm_review`, `recent_decisions` | mémoire courte anti-répétition |
| `structure` | seulement pour le symbole décidé : timeframe des barres, fraîcheur/compte, prix, swings exacts 24/48, ATR normalisé et volume relatif |
| `company_intelligence` | tranche fraîche micro/news du symbole, avec couverture et autorité de recherche |
| `universe_mandate` | raison de sélection et posture émises par Univers, sans autorité d'exécution |

## Plans et veilles — 3 niveaux

Le contexte plans/watches est volontairement étagé :

| Niveau | Champ / outil | Portée | Contenu |
|---|---|---|---|
| Détail local | `per_symbol[sym].active_watches` | symbole décidé seulement | veilles et plans armés actifs du symbole, avec conditions et expiration |
| Résumé focalisé | `shared_context.active_plans_summary` | cible + compteurs portefeuille | total, nombre de symboles, compteurs par kind et plans compacts du symbole courant |
| Détail global | `get_active_plans` | portefeuille, à la demande | vrais `TradePlan` ouverts sérialisés, retour `{rows, as_of}` ; `symbol` filtre mais ne limite pas au symbole courant |

Le résumé focalisé sert surtout d'anti-doublon/OCO avant d'empiler des scénarios.
L'outil `get_active_plans` ne sert que si le détail local et ce résumé ne
suffisent pas.

Le détail global reste une projection opérationnelle bornée : il expose les
règles de sortie persistées (`hard_stop`, TP, trailing, protection de gain,
max-hold, exit-watch) et leur suivi (watermarks, TP remplis, volatilité de
référence, expiration/cooldown de watch), mais jamais le gros `entry_context`
ou un payload auxiliaire de watch. Les limites et indicateurs explicites de
troncature sont définis dans [agent-tools](agent-tools.md#get_active_plans) ;
un flag `*_truncated` interdit d'inférer qu'une règle absente n'existe pas.

## Learnings : compétence, situation, expérience

- `learnings.global` + `guardrails` restent poussés : compétence générale lente.
- `company_intelligence` + `universe_mandate` portent la situation courante du nom.
- Les anciens slots `learnings.by_symbol` ne sont plus poussés en queue : une
  expérience historique n'est pas une situation actuelle.
- `recall_learnings` permet d'approfondir à la demande avec une query de setup.
  FLAIR pondère l'outcome propre de la note ; MemRL pondère l'utilité observée de
  ses rappels dans des décisions ultérieures.
- Chaque règle de `learnings.global` porte un `rule_id`. L'agent peut citer au
  plus trois règles réellement appliquées via `applied_learning_ids`; la citation
  est validée contre les IDs effectivement présents dans son prompt.

`indicator_triggers` dit "une condition s'est réalisée". `wake_reasons` dit
"le scheduler t'a réveillé pour réviser un état", par exemple parce que le TTL
d'une veille est terminé.

## Horodatage (`now`, `now_human`, `market_clocks`)

Trois repères temporels, tous dérivés d'un `now` **UTC-aware**
(`datetime.now(timezone.utc)`, `daemon.py` `main()`) — jamais l'heure locale de la
machine :

- **`now`** : ISO 8601 UTC (`2026-07-04T07:16:00+00:00`) — repère machine.
- **`now_human`** (`market.human_clock`) : jour FR + date + heure **UTC**
  (`"vendredi 04/07 07:16 UTC"`), déterministe et locale-indépendant.
- **`market_clocks`** (`market.market_clocks`) : heure **locale de chaque place**
  présente dans l'univers du cycle, dédupliquée par fuseau et triée ouest→est
  (`"New York ven 03:16 · Paris ven 09:16 · Taipei ven 15:16"`). Le jour est affiché
  **par zone** : il peut différer de l'UTC (ex. **samedi à Taipei** alors qu'il est
  encore vendredi UTC) → lève l'ambiguïté week-end/ouverture.

Le raisonnement est ancré sur UTC (neutre pour du multi-marchés) ; les sessions
(`session_snapshot`) utilisent la tz de **chaque place**, jamais celle de l'hôte. Un
daemon opéré depuis n'importe quel fuseau (ex. Taiwan) produit donc le même contexte.

## Invariant

Le cockpit porte des **faits** (régime, devise/fx) ; l'âge data, la session, les
triggers et les raisons de réveil sont ajoutés séparément (`planner_batch`). La prose stratégique vit dans le
[mandat](llm-contract.md), pas ici (AX « code over instructions »).

## Voir aussi
- [Contrat LLM](llm-contract.md) (consommateur) · [Régime](regime.md) · [Sémantique](semantic.md) · Architecture §3.3.
