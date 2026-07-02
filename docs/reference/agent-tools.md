# Référence — Domain tools (la tournée d'outils du LLM)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent_tools/` · **Registry** : `agent_tools/registry.TOOL_REGISTRY`
> **Activation** : `CASYS_AGENT_TOOLS_ENABLED=1` · **Spec** : `docs/superpowers/specs/2026-06-29-agent-domain-tools-design.md`

Le LLM décideur peut **pull** du contexte supplémentaire via des outils
**lecture-seule**, en émettant `{"tool_calls":[...]}` dans sa réponse (protocole
JSON dans le prompt, PAS les outils natifs de codex — la décision tourne avec
`--allowed-tools ""`). Le daemon exécute, réinjecte les résultats, et le LLM
statue.

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
