# Référence — Domain tools (la tournée d'outils du LLM)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent_tools/` · **Registry** : `agent_tools/registry.TOOL_REGISTRY`
> **Activation** : `CASYS_AGENT_TOOLS_ENABLED=1` · **Spec** : `docs/superpowers/specs/2026-06-29-agent-domain-tools-design.md`

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

## Les 9 outils

| Outil | Module | Rôle |
|---|---|---|
| `get_freshness` | `freshness` | âge/fraîcheur des données par symbole |
| `get_indicator_context` | `indicators` | contexte indicateurs d'un symbole |
| `describe_data` | `indicators` | décrit les données/indicateurs disponibles |
| `find_indicators` | `indicators` | recherche d'indicateurs (couche sémantique) |
| `get_active_plans` | `plans` | plans armés / veilles actives |
| `get_position_risk` | `risk` | risque d'une position (exposition, distance stop) |
| `get_attribution` | `attribution` | attribution d'un trade / round-trip |
| `get_recent_decisions` | `attribution` | décisions récentes d'un symbole |
| `recall_learnings` | `learnings` | rappel sémantique de learnings (cf. [RAG](learnings-rag.md)) |

Chaque outil = un `ToolSpec(name, validate_args, handler)` enregistré dans
`TOOL_REGISTRY` (`agent_tools/registry`).

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
| `propose_order` | `action` / `quantity` / `intent` + `exit_plan` | compile une intention ; le daemon valide puis RiskGate/broker |
| `set_next_wake` | `next_wake_in_minutes` | planifie le prochain réveil du symbole |
| `propose_indicator_watch` | `indicator_watch` | pose une veille via le scheduler |
| `cancel_watch` | `cancel_watch_ids` | annule seulement les veilles possédées par le symbole |
| `record_learning` | `learning` | borne et persiste une note runtime |

**Side de `propose_order`** : `OPEN_LONG`→BUY et `OPEN_SHORT`→SELL sont déduits
automatiquement. **`CLOSE`/`REDUCE`/`REVERSE` dérivent aussi la side depuis la
position au portefeuille (L2, Phase 6)** : `CLOSE` ferme toute la position (qty
omise ou ignorée) ; `REDUCE` accepte `fraction:0.5` ou `qty` absolue ; `REVERSE`
dérive la side mais requiert `qty` (nouvelle jambe). Si `side:BUY|SELL` est fourni
explicitement, il est utilisé tel quel (compat). Fail-safe : position=0 → HOLD
tracé `nothing_to_close`.

Vocabulaire compact de `propose_order.args.exit` :

| Compact | Interne |
|---|---|
| `stop` | `hard_stop` |
| `tp[{r,...}]` | `take_profits[{type:"risk_multiple", r,...}]` |
| `trail{type,value}` | `trailing_stop{trail_type, trail_value}` |
| `protect.arm_r` | `profit_protection.arm_at_r` |
| `protect.giveback` | `profit_protection.trigger_on_giveback_pct` |
| `protect.lock_r` | remonte le stop à `entry +/- lock_r * R` au déclenchement |

Aliases compat : `after_r` / `enabled_after_r` / `activate_after_r` -> `arm_r`,
`protect_r` / `lock_in_r` -> `lock_r`.

## Contrat d'exécution — `execute_tool_round`

`agent_tools/core.execute_tool_round(raw_calls, *, context, limits, allowed_tools)`
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
- [RAG / learnings](learnings-rag.md) · [reporting](reporting.md) · Architecture §10.
