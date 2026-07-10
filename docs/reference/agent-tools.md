# Référence — Domain tools (la tournée d'outils du LLM)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent/tools/` · **Registry** : `agent/tools/registry.TOOL_REGISTRY`
> **Activation** : `CASYS_AGENT_TOOLS_ENABLED=1` · **Historique du design** : voir `git log` (specs 2026-06-29 / 2026-07-03 supprimées une fois livrées, cette page fait foi)

Le LLM décideur peut **pull** du contexte supplémentaire via des outils
**lecture-seule**, en émettant `{"tool_calls":[...]}` dans sa réponse (protocole
JSON dans le prompt, PAS les outils natifs de codex — la décision tourne avec
`--allowed-tools ""` par défaut ; l'`exec` natif optionnel reste une capacité
séparée). Le daemon exécute, réinjecte les résultats, et le LLM statue.

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
| `get_active_plans` | `plans` | détail complet TradePlans ouverts (portée globale) |
| `get_attribution` | `attribution` | attribution d'un trade / round-trip |
| `recall_learnings` | `learnings` | rappel sémantique de learnings (cf. [RAG](learnings-rag.md)) |

Chaque outil = un `ToolSpec(name, validate_args, handler)` enregistré dans
`TOOL_REGISTRY` (`agent/tools/registry`).

`get_indicator_context` renvoie par défaut tous les indicateurs gouvernés demandés.
Le catalogue fini est la borne naturelle ; l'agent n'a pas à découper une recherche
multi-indicateurs en plusieurs appels. Les bornes techniques portent sur le nombre
de tool calls et de requêtes de données, pas sur quatre ou six indicateurs arbitraires.
Le résultat porte aussi un bloc `structure` : prix, vraie date de la dernière
barre, nombre de barres, swings exacts 24/48, `atr_pct_14` et
`relative_volume_20`.

### Calcul natif en cage (`exec`)

Quand `CASYS_AGENT_EXEC=1`, le transport Codex expose aussi shell/Python dans le
scratch du `CODEX_HOME` isolé : réseau coupé et écritures confinées hors du repo.
Ce n'est pas un domain tool et cela ne contourne ni le `RiskGate`, ni le contrat
de décision : l'agent peut s'en servir pour un calcul déterministe ad hoc
(corrélation, distance à un niveau, retracements de Fibonacci à partir d'ancres
connues), mais sa réponse finale reste du JSON pur et l'exécution de l'ordre reste
daemon-owned. Un calcul libre n'est pas automatiquement une condition de watch
persistante ; une logique récurrente doit être exprimée avec le catalogue gouverné
ou promue dans la semantic layer pour rester rejouable et auditable.

### `get_active_plans`

`get_active_plans{symbol?, limit?}` retourne toujours un objet
`{"rows": [...], "as_of": ...}`.

En queue runtime (mode prod), `rows` vient de `ToolContext.open_plans_provider` :
ce sont de vrais `TradePlan` ouverts sérialisés, pas les watches locales. La
portée est volontairement globale pour permettre la conscience portefeuille ;
`symbol` est un filtre optionnel, pas une limite implicite au symbole courant.
`limit` est plafonné à 20. `as_of` reprend l'horodatage du snapshot de plans.

Sans provider (fallback batch), le handler retombe sur `active_watches_by_symbol`
et rend des watches avec `as_of: null`. Ce mode est compatibilité/dégradé ; la
référence opératoire est le provider global de plans ouverts.

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
          "tool": "strategy_entry",
          "args": {
            "id": "long",
            "direction": "long",
            "qty": 20,
            "exit": {
              "id": "bracket",
              "limit": 78.5,
              "stop": {"struct": "swing_low", "window": 24},
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
| `strategy_entry` | `direction` / `qty` ou `risk_pct` / `exit` | `position intent` Pine-like : `direction:"long|short"` ouvre si flat, renforce si même sens, retourne si sens opposé ; le daemon compile ensuite vers `action` / `intent` / `exit_plan` internes puis valide via RiskGate/broker. Options : `qty`, `risk_pct`, `exit`, `thesis` |
| `strategy_exit` | plan de sortie / amendement de sortie | `exit rule` : patche le plan de sortie d'une position **déjà ouverte** (`limit`, `stop`, `trail`, `protect`, `exit_watch`) sans fermer/rouvrir ; rejet tracé si pas de plan ouvert. Peut coexister avec `calls:[]` (HOLD + gestion active) |
| `strategy_close` | fermeture / réduction position-aware | sortie marché immédiate : sans taille ferme toute la position ; `qty_percent<100` réduit une fraction ; `qty` réduit une quantité absolue |
| `set_next_wake` | `next_wake_in_minutes` | planifie la **reconsultation** du symbole : `{minutes}` (timer), `{on: session_open\|macro_event\|pre_earnings}` (événement calendaire) ou `{when:<condition>}` (réveil-sur-indicateur, compilé en `WAKE`) |
| `propose_indicator_watch` | `indicator_watch` | pose une veille/plan armé via le scheduler (`WAKE` = réveil de reconsultation ; `EXECUTE_ORDER` = **plan armé** exécuté sans reconsulter) |
| `cancel_watch` | `id` / `ids` / `watch_ids` | annule seulement les veilles possédées par le symbole |
| `record_learning` | `learning` | borne et persiste une note runtime |

**Grammaire Pine-like JSON officielle** :

- `position intent` = changer l'exposition (`strategy_entry`) : ouvrir une
  nouvelle jambe longue ou short.
- `exit rule` = règle attachée à une position ouverte (`strategy_entry.exit` à
  l'entrée, puis `strategy_exit` pour patcher) : stop, TP, trailing, protection
  de gain, review/temps.
- `strategy_close` = sortie marché immédiate position-aware : fermer ou réduire
  l'exposition existante sans fournir de side/action.
- `review wake` = reconsultation par le LLM (`set_next_wake`) : timer, événement
  calendaire ou condition indicateur qui réveille l'agent.
- `armed plan` = exécution daemon sans reconsultation (`propose_indicator_watch`
  en `EXECUTE_ORDER`) : scénario armé qui repasse quand même par les gates
  déterministes.

**Modèle de sortie** : l'inspiration publique est `strategy.exit` de Pine Script,
mais en JSON MCP plutôt qu'en script. `strategy_exit.limit` = take-profit absolu,
`strategy_exit.stop` = stop absolu ou structurel, et `limit+stop` dans
`strategy_exit` signifie **bracket de sortie** (TP + stop), pas stop-limit.
`qty_percent` omis = 100 %. Pour l'instant, `qty_percent` ne s'applique qu'à
`limit` seul pour un scale-out. `limit+stop` avec `qty_percent<100` est rejeté
pour l'instant
(`partial_bracket_exit_not_supported`) tant que la réservation/OCA partielle
n'est pas native ; `stop` avec `qty_percent<100` est rejeté
(`partial_stop_exit_not_supported`). Un `strategy_exit.stop` sur position déjà
ouverte peut aussi servir de protection de gain sous un swing récent. Dans ce
dernier cas, le daemon accepte un stop au-dessus de l'entrée d'un long seulement
s'il reste sous le prix courant ; symétriquement, un short doit garder le stop
au-dessus du prix courant. En mode feedback pré-exécution, l'agent peut recevoir
un `tool_results` `{tool:"strategy_exit", ok:false, error:<reason>}` de validation pré-exécution ;
il doit alors corriger dans la même réponse (stop en prix absolu ou retrait de la
contrainte non résolvable). Cette boucle ne concerne que `strategy_exit`, est
bornée à 2 corrections, puis le runtime laisse la décision finale suivre le
chemin normal. L'agent n'a pas besoin de redemander le plan : il est déjà dans
son contexte (`active_watches`, `active_plans_summary` ou `get_active_plans` si
le détail global a été demandé).

**Réveil vs plan armé** : `set_next_wake` = **reconsultation** (l'agent reprend la main pour redécider). Avec `{when:<condition>}`, il est compilé en `indicator_watch{on_trigger:WAKE}`. `propose_indicator_watch{on_trigger:EXECUTE_ORDER}` = **automatisation** (le daemon exécute sans reconsulter l'agent). `set_next_wake{when}` et `propose_indicator_watch` dans la même décision sont rejetés comme ambigus.

**Position-aware** : `strategy_entry.direction:"long"` déduit BUY et
`direction:"short"` déduit SELL. Si le symbole est flat, l'intention interne
reste `OPEN_LONG`/`OPEN_SHORT`. Si la position existe déjà dans le même sens, le
daemon compile en renforcement interne (`SCALE_IN`) ; si elle existe dans l'autre sens,
il compile en retournement interne (`FLIP`). `risk_pct` sans `qty` est réservé
à une nouvelle entrée flat ; pour renforcer/retourner une position existante,
fournis `qty`. `strategy_close` dérive la side depuis la position au portefeuille :
sans taille il ferme toute la position ; avec `qty_percent<100` il réduit une
fraction ; avec `qty` il réduit une quantité absolue. Fail-safe : position=0 →
HOLD tracé `nothing_to_close`.
Pour une nouvelle entrée avec bracket/protection, les règles de sortie vont dans
`strategy_entry.exit`. `strategy_exit` est réservé aux positions déjà ouvertes ;
`strategy_entry + strategy_exit` sur le même symbole est rejeté.

**`thesis` de `strategy_entry`** *(optionnel, L6)* : `{setup, horizon:"intraday|swing|position",
invalidation}` — tag **structuré** persisté sur la décision (`decisions.jsonl`) pour
l'attribution et le RAG learnings (corréler setup → résultat). Fail-safe : un thesis
malformé/partiel est ignoré (jamais de décision cassée). Branchement RAG = backlog.

Vocabulaire compact de `strategy_entry.args.exit` et `strategy_exit.args` :

| Compact | Interne |
|---|---|
| `limit` | `take_profits[{type:"price", price, fraction}]` |
| `stop` | `hard_stop` |
| `tp[{r,...}]` | `take_profits[{type:"risk_multiple", r,...}]` |
| `trail{type,value}` | `trailing_stop{trail_type, trail_value}` |
| `protect.arm_r` | `profit_protection.arm_at_r` |
| `protect.giveback` | `profit_protection.trigger_on_giveback_pct` |
| `protect.lock_r` | remonte le stop à `entry +/- lock_r * R` au déclenchement |

**Contrat strict** : les seuls action tools d'ordre/sortie acceptés au runtime
sont `strategy_entry`, `strategy_exit` et `strategy_close`. `exit_update` reste un
champ interne de décision/ledger produit par `strategy_exit`, pas un outil agent.
Les anciens états doivent être migrés avant d'être relus comme historique
durable.

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

- En batch legacy, offert **seulement au 1er passage** de décision
  (`allow_tool_calls = agent_tools_enabled AND allow_context_request`), pas après
  un `REQUEST_CONTEXT` ni au tour final.
- En queue grain-1, `resolve_symbol_decision` peut enchaîner des rounds dans une
  session acpx persistante jusqu'à décision finale ; le backstop est technique,
  pas un budget fonctionnel.
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
