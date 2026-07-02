# Référence — Contexte agent (le cockpit envoyé au LLM)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent/context` · **Rôle** : construit le contexte compact + la recherche d'indicateurs bornée pour l'agent.

`build_market_cockpit` construit le dict **`cockpit`** — **une clé** du
`shared_context`. Le `shared_context` complet est assemblé dans `runtime/daemon`
(il ajoute `now`, `portfolio`, `risk_limits`/`risk_capacity`, `kpis`, `attribution`,
`meta_performance`, `learnings`, `regime_families`, `semantic`, `stale_market_data`).
Ce contexte est injecté **une fois** dans le prompt batch (cf.
[llm-contract](llm-contract.md)) : faits calculés par le code, pas de prose (AX).

## `build_market_cockpit(...)`

Construit le `cockpit` : par symbole, prix, indicateurs **15m + daily**, régime,
signaux HTF, distances swing, frais, **devise/fx/budgets** — compacté
(`_compact_price`, arrondis). ⚠️ **`data_age_m` et `session` ne viennent PAS d'ici** :
ils sont calculés par `application/planner_batch._symbol_facts()` et injectés
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
pour maîtriser le coût (cf. `application/planner_batch`).

## Invariant

Le cockpit porte des **faits** (régime, devise/fx) ; l'âge data et la session sont
ajoutés séparément (`planner_batch`). La prose stratégique vit dans le
[mandat](llm-contract.md), pas ici (AX « code over instructions »).

## Voir aussi
- [Contrat LLM](llm-contract.md) (consommateur) · [Régime](regime.md) · [Sémantique](semantic.md) · Architecture §3.3.
