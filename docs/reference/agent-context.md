# Référence — Contexte agent (le cockpit envoyé au LLM)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent/context` · **Rôle** : construit le contexte compact + la recherche d'indicateurs bornée pour l'agent.

`build_market_cockpit` construit le dict **`cockpit`** — **une clé** du
`shared_context`. Le `shared_context` complet est assemblé dans `runtime/daemon`
(il ajoute `now`, `now_human`, `market_clocks`, `portfolio`,
`risk_limits`/`risk_capacity`, `kpis`, `attribution`, `meta_performance`,
`learnings`, `regime_families`, `semantic`, `stale_market_data`,
`active_plans_summary`).
Ce contexte est injecté **une fois** dans le prompt batch (cf.
[llm-contract](llm-contract.md)) : faits calculés par le code, pas de prose (AX).

## `portfolio` — cash ledger vs cash libre

`portfolio.cash`/`portfolio.cash_ledger` est le ledger broker paper. Une vente
short crédite ce ledger, comme une comptabilité de fill, mais ce montant ne doit
pas être lu comme du cash mobilisable. `portfolio.cash_available` retire
l'exposition short courante (`short_exposure_usd`) et sert à l'affichage humain.
Pour dimensionner une nouvelle ouverture, l'agent lit surtout
`risk_capacity.gross_remaining_usd` et les plafonds `max_*_qty`.

## `build_market_cockpit(...)`

Construit le `cockpit` : par symbole, prix, indicateurs **15m + daily**, régime,
signaux HTF, distances swing, frais, **devise/fx/budgets** — compacté
(`_compact_price`, arrondis). ⚠️ **`data_age_m` et `session` ne viennent PAS d'ici** :
ils sont calculés par `application/decide/planner_batch._symbol_facts()` et injectés
séparément par symbole (`per_symbol_payload`).

- `_swing_distances_pct(...)` — distances aux niveaux swing en **fraction du prix**
  (0–1, ex. 5 % = `0.05` — direct compatible `min_pct`/`max_pct` des stops).
- `_family_code(value)` — code famille compact.

C'est la **source du « push complet »** : si le cockpit suffit, l'agent décide
directement sans pull (cf. [agent-tools](agent-tools.md) — usage outils rare
by-design).

## `resolve_indicator_requests(...)`

Résout une **demande de contexte bornée** (`REQUEST_CONTEXT`) : quand l'agent
demande des indicateurs supplémentaires, cette fonction les calcule dans les limites
(`max_requests`, `max_indicators`) et les réinjecte au 2e passage de décision. Borné
pour maîtriser le coût (cf. `application/decide/planner_batch`).

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

## Plans et veilles — 3 niveaux

Le contexte plans/watches est volontairement étagé :

| Niveau | Champ / outil | Portée | Contenu |
|---|---|---|---|
| Détail local | `per_symbol[sym].active_watches` | symbole décidé seulement | veilles et plans armés actifs du symbole, avec conditions et expiration |
| Résumé global | `shared_context.active_plans_summary` | portefeuille | résumé compact construit par `runtime/daemon._global_plans_summary` : `symbol`, `id`, `kind` (`armed`/`wake`) et `intent` si disponible, sans conditions |
| Détail global | `get_active_plans` | portefeuille, à la demande | vrais `TradePlan` ouverts sérialisés, retour `{rows, as_of}` ; `symbol` filtre mais ne limite pas au symbole courant |

Le résumé global sert surtout d'anti-doublon/OCO avant d'empiler des scénarios.
L'outil `get_active_plans` ne sert que si le détail local et ce résumé global ne
suffisent pas.

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
