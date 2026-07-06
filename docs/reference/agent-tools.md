# Référence — Domain tools (la tournée d'outils du LLM)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent/tools/` · **Registry** : `agent/tools/registry.TOOL_REGISTRY`
> **Activation** : `CASYS_AGENT_TOOLS_ENABLED=1` · **Historique du design** : voir `git log` (specs 2026-06-29 / 2026-07-03 supprimées une fois livrées, cette page fait foi)

Le LLM décideur peut **pull** du contexte supplémentaire via des outils
**lecture-seule**, en émettant `{"tool_calls":[...]}` dans sa réponse (protocole
JSON dans le prompt, PAS les outils natifs de codex — la décision tourne avec
`--allowed-tools ""`). Le daemon exécute, réinjecte les résultats, et le LLM
statue.

En mode `CASYS_AGENT_TOOLS_ENABLED=1`, le contrat final visible peut aussi être
un langage **tools par symbole** : chaque symbole rend `calls`, et ces calls sont
compilés par le daemon vers les primitives internes (`Decision`, `exit_plan`,
veille, réveil, learning) avant les gates existants. Les anciens champs restent
acceptés en compat cachée, mais ne sont pas exposés dans ce contrat.

## Les 7 outils read-only

| Outil | Module | Rôle |
|---|---|---|
| `get_freshness` | `freshness` | âge/fraîcheur des données par symbole |
| `get_indicator_context` | `indicators` | contexte indicateurs d'un symbole |
| `describe_data` | `indicators` | décrit les données/indicateurs disponibles |
| `find_indicators` | `indicators` | recherche d'indicateurs (couche sémantique) |
| `get_active_plans` | `plans` | plans armés / veilles actives |
| `get_attribution` | `attribution` | attribution d'un trade / round-trip |
| `recall_learnings` | `learnings` | rappel sémantique de learnings (cf. [RAG](learnings-rag.md)) |

Chaque outil = un `ToolSpec(name, validate_args, handler)` enregistré dans
`TOOL_REGISTRY` (`agent/tools/registry`).

## Action tools finaux par symbole

Format agent visible :

```json
{
  "decisions": [
    {
      "symbol": "DASH",
      "confidence": 0.74,
      "rationale": "breakout propre",
      "decision_reason_code": "ENTRY_SIGNAL",
      "calls": [
        {
          "tool": "propose_order",
          "args": {
            "intent": "OPEN_LONG",
            "qty": 20,
            "exit": {
              "stop": {"struct": "swing_low", "window": 24},
              "tp": [{"r": 1.4, "fraction": 0.5}],
              "protect": {"arm_r": 1.0, "giveback": 0.35, "lock_r": 0.25}
            }
          }
        },
        {"tool": "set_next_wake", "args": {"minutes": 15}}
      ]
    }
  ]
}
```

`calls: []` signifie HOLD explicite. `id` est optionnel : le daemon en génère un
stable si besoin pour `runtime.tool_calls`.

Action tools acceptés :

| Tool | Remplace | Effet réel |
|---|---|---|
| `propose_order` | `action` / `quantity` / `intent` + `exit_plan` | `position intent` : compile une intention (`intent` OPEN_LONG/OPEN_SHORT/REDUCE/CLOSE/REVERSE/ADD) ; le daemon valide puis RiskGate/broker. Options : `thesis` (L6), side/qty position-aware (L2, ci-dessous) |
| `amend_exit` | — *(nouveau, L3)* | `exit rule` : patche le plan de sortie d'une position **déjà ouverte** (`stop`/`tp`/`trail`/`protect`, même grammaire canonique que `propose_order.exit`) sans fermer/rouvrir ; no-op tracé si pas de plan ouvert. Peut coexister avec `calls:[]` (HOLD + gestion active) |
| `set_next_wake` | `next_wake_in_minutes` | planifie la **reconsultation** du symbole : `{minutes}` (timer), `{on: session_open\|macro_event\|pre_earnings}` (événement calendaire) ou `{when:<condition>}` (réveil-sur-indicateur, compilé en `WAKE`) |
| `propose_indicator_watch` | `indicator_watch` | pose une veille/plan armé via le scheduler (`WAKE` = réveil de reconsultation ; `EXECUTE_ORDER` = **plan armé** exécuté sans reconsulter) |
| `cancel_watch` | `cancel_watch_ids` | annule seulement les veilles possédées par le symbole |
| `record_learning` | `learning` | borne et persiste une note runtime |

**Typologie trading officielle** :

- `position intent` = changer l'exposition (`propose_order`) : ouvrir, renforcer,
  réduire, fermer ou retourner une position.
- `exit rule` = règle attachée à une position ouverte (`propose_order.exit` à
  l'entrée, puis `amend_exit` pour patcher) : stop, TP, trailing, protection de
  gain, review/temps.
- `review wake` = reconsultation par le LLM (`set_next_wake`) : timer, événement
  calendaire ou condition indicateur qui réveille l'agent.
- `armed plan` = exécution daemon sans reconsultation (`propose_indicator_watch`
  en `EXECUTE_ORDER`) : scénario armé qui repasse quand même par les gates
  déterministes.

**Modèle de sortie** : à terme, `stop`, `tp`, `trail` et `protect` doivent se
lire comme des règles de sortie composées d'un déclencheur et d'une action. Le
protocole garde encore des champs distincts pour compatibilité, mais ils portent
la même famille de logique : un `tp` peut être en R ou en prix, un `stop` peut
être structurel ou absolu, et un `amend_exit.stop` sur position déjà ouverte peut
servir de protection de gain sous un swing récent. Dans ce dernier cas, le daemon
accepte un stop au-dessus de l'entrée d'un long seulement s'il reste sous le prix
courant ; symétriquement, un short doit garder le stop au-dessus du prix courant.

**Réveil vs plan armé** : `set_next_wake` = **reconsultation** (l'agent reprend la main pour redécider). Avec `{when:<condition>}`, il est compilé en `indicator_watch{on_trigger:WAKE}`. `propose_indicator_watch{on_trigger:EXECUTE_ORDER}` = **automatisation** (le daemon exécute sans reconsulter l'agent). `set_next_wake{when}` et `propose_indicator_watch` dans la même décision sont rejetés comme ambigus.

**Side de `propose_order`** : `OPEN_LONG`→BUY et `OPEN_SHORT`→SELL sont déduits
automatiquement. **`CLOSE`/`REDUCE`/`REVERSE` dérivent aussi la side depuis la
position au portefeuille (L2, Phase 6)** : `CLOSE` ferme toute la position (qty
omise ou ignorée) ; `REDUCE` accepte `fraction:0.5` ou `qty` absolue ; `REVERSE`
dérive la side mais requiert `qty` (nouvelle jambe). Si `side:BUY|SELL` est fourni
explicitement, il est utilisé tel quel (compat). Fail-safe : position=0 → HOLD
tracé `nothing_to_close`.

**`thesis` de `propose_order`** *(optionnel, L6)* : `{setup, horizon:"intraday|swing|position",
invalidation}` — tag **structuré** persisté sur la décision (`decisions.jsonl`) pour
l'attribution et le RAG learnings (corréler setup → résultat). Fail-safe : un thesis
malformé/partiel est ignoré (jamais de décision cassée). Branchement RAG = backlog.

Vocabulaire compact de `propose_order.args.exit` :

| Compact | Interne |
|---|---|
| `stop` | `hard_stop` |
| `tp[{r,...}]` | `take_profits[{type:"risk_multiple", r,...}]` |
| `trail{type,value}` | `trailing_stop{trail_type, trail_value}` |
| `protect.arm_r` | `profit_protection.arm_at_r` |
| `protect.giveback` | `profit_protection.trigger_on_giveback_pct` |
| `protect.lock_r` | remonte le stop à `entry +/- lock_r * R` au déclenchement |

**Compat cachée** : le parser conserve des aliases historiques (`hard_stop`,
`stop_loss`, `sl`, `take_profits`, `profit_protection`, `after_r`,
`enabled_after_r`, `activate_after_r`, `protect_r`, `lock_in_r`) pour absorber les
réponses LLM et les traces legacy. Ils ne sont pas le vocabulaire recommandé :
la forme agent officielle reste `stop`, `tp`, `trail`, `protect`.

**OCO ratchet (L7)** : un take-profit avec `after_fill:"move_stop_to_tp"` déplace le
`hard_stop` au niveau du TP au moment du fill (monotone — ne rétrograde jamais un stop
déjà plus protecteur) ; sur un plan multi-TP, le stop s'égrène automatiquement de TP en
TP. (`after_fill:"move_stop_to_breakeven"` remonte au prix d'entrée.) L'exit engine a
déjà un OCO implicite : une fermeture totale rend caducs les autres ordres du plan.

## Contrat d'exécution — `execute_tool_round`

`agent/tools/core.execute_tool_round(raw_calls, *, context, limits, allowed_tools)`
→ **UNE tournée bornée** : chaque call rend TOUJOURS un `(result, trace)`, même
rejeté ou hors budget (le LLM voit ce qui s'est passé).

- **Bornes** : `ToolRoundLimits(max_total_calls=24, max_calls_per_symbol=3)`. Le cap
  de la liste d'entrée est une constante module `_MAX_RAW_CALLS=32` (surplus tronqué
  avec une trace sentinelle) — distinct des bornes ci-dessus.
- **Validation** : `validate_tool_call` (nom autorisé + args) avant exécution ;
  rejet = trace `rejected:<reason>`.
- **Trace** : `round_runtime_payload(traces, *, rounds)` (`rounds` keyword-only)
  persiste `runtime.tool_rounds` + `runtime.tool_calls` dans `decisions.jsonl`.

## Où c'est branché (gates)

- Offert **seulement au 1er passage** de décision (`allow_tool_calls =
  agent_tools_enabled AND allow_context_request`), pas après un `REQUEST_CONTEXT`
  ni au tour final.
- En mode `use_symbol_calls_contract` (le langage `calls:[...]`), le contrat du
  **1er tour laisse le choix** à l'agent : émettre `{"tool_calls":[...]}` (pull
  read-only) OU rendre directement `{"decisions":[...]}` ; le tour final impose
  `decisions`. Correctif 9bbfa6a : le « Réponds UNIQUEMENT » d'origine neutralisait
  la tournée read-only pourtant offerte au même passage.
- Budget réservé (`budget // 2` quand actif).
- Catalogue (`_TOOL_CATALOG`) injecté dans le prompt **seulement si** `allow_tool_calls`.

## Comportement observé

Usage réel **rare** (~0 à ce jour) : le prompt dit « si le cockpit suffit, rends
directement le contrat final », et le push est complet → l'agent pull peu. C'est
**by-design** (escape hatch pour cas limites), pas un dead-path : le mécanisme
frère `context_request` s'exerce, le câblage est vérifié vivant. Cf. discussion
paradigme push-complet dans le registre / analyses.

## Voir aussi
- [Scheduler / watches](wake-scheduler.md) · [RAG / learnings](learnings-rag.md) · [reporting](reporting.md) · Architecture §10.
