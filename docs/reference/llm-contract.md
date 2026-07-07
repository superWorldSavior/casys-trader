# Référence — Contrat LLM : prompts & mandat

> **Type** : Reference (Diátaxis).
> **Code** : `trader/agent/protocol/prompts.py` (assemblage), `trader/agent/protocol/parsing.py` (validation), `trader/agent/protocol/types.py` · **Mandat** : `mandate/`
> **Rôle** : comment on parle au LLM décideur, et comment on parse sa réponse.

Le protocole est **prompt-based** : tout (contexte, contrat de sortie, vocabulaire
des veilles, catalogue d'outils) est dans le texte du prompt ; la réponse est du
**JSON strict** parsé et validé. Le transport (`agent/llm`, `agent/client`) est
séparé.

## Le prompt batch — `build_batch_prompt(...)`

Un seul appel modèle pour tout l'univers dû. Le **contexte partagé** (cockpit,
portefeuille, KPI, attribution, learnings) est envoyé **UNE fois**, puis la liste
des symboles à décider. Ordre d'assemblage :

1. Cadrage **PLANIFICATEUR** : « tu conçois des scénarios (entrées armées, veilles,
   plans de sortie) que le daemon exécute mécaniquement ; tu n'opères pas le marché
   en continu ».
2. `# Mandat` — `mandate/mandate.md` (voir plus bas).
3. `# Mémoire / stratégie` — `mandate/memory.md`.
4. `_DECISION_GUIDANCE` — échelle d'engagement, guidance de décision.
5. `_indicator_watch_vocabulary()` — vocabulaire des veilles (indicateurs, opérateurs,
   `on_trigger` WAKE/EXECUTE_ORDER, logic any/all).
6. `_TOOL_CATALOG` — **seulement si** `allow_tool_calls` (cf. [agent-tools](agent-tools.md)).
7. `# Contexte partagé (JSON)` — `shared_context`.
8. `# Symboles à décider (JSON)` — `symbols_payload`.
9. `# Contrat de sortie` — contrat final `symbol_calls` canonique. Le flag
   `use_symbol_calls_contract` reste accepté pour compatibilité d'appel mais ne
   réactive plus l'ancien schéma inline.

Enums injectés (source de vérité côté code) : indicateurs de veille
(`DEFAULT_INDICATORS`), opérateurs (`WATCH_VALID_OPERATORS`), codes de raison
(`trader.domain.decision_reason.reason_code_enum_text()`).

## Le contrat de sortie

JSON strict par symbole :
`{"symbol","confidence","rationale","decision_reason_code","calls":[...]}`.
`calls: []` signifie HOLD explicite.

La grammaire de trading publique est Pine-like JSON :
`strategy_entry`, `strategy_exit`, `strategy_close`, plus
`set_next_wake`, `propose_indicator_watch`, `cancel_watch`, `record_learning`.
Le parser compile ces calls vers les primitives internes `Decision`
(`action`, `quantity`, `intent`, `exit_plan`, `exit_update`, veilles, learnings).

Le chemin moderne de recherche de contexte passe par les tool rounds read-only
(`get_indicator_context`, `get_active_plans`, etc.). Le chemin single-symbol garde
encore `REQUEST_CONTEXT` en compatibilité parser, mais le batch/queue runtime expose
la grammaire `calls`.

### Feedback pré-exécution `strategy_exit`

En queue grain-1, un `strategy_exit` final est dry-run avant application. Si ce
dry-run serait rejeté, le modèle reçoit dans la même session un
`tool_results` d'action en erreur :

```json
{"tool": "strategy_exit", "ok": false, "error": "<reason>"}
```

Ce feedback n'est pas une exécution partielle : c'est une validation
pré-exécution. L'agent doit corriger sa réponse suivante (par exemple prix de
stop absolu au lieu d'un stop structurel non résolvable) ou retirer la contrainte.
Il n'a pas besoin de redemander le plan : le plan ouvert est déjà visible via le
contexte local/global, et `get_active_plans` ne sert qu'à récupérer un détail
global manquant. La boucle est bornée à 2 corrections ; `strategy_entry` et
`strategy_close` restent validés par les gates daemon, hors de ce feedback.

Parsé/validé par `agent/protocol/parsing` → toute réponse douteuse **dégrade en
HOLD** (fail-safe, cf. `codex_client`).

## Le mandat — `mandate/mandate.md`

Édité en **boucle 1** (Erwan + Claude, humain), **relu à chaque réveil** par
l'agent runtime. Sections : Objectif · Marchés autorisés · Contraintes · KPI suivis
· Indicateurs · Plan de sortie · **« Ce qui n'est PAS dans le mandat (volontairement) »**.

- `mandate/memory.md` — mémoire/stratégie longue (boucle 1).
- `mandate/guardrails.json` — garde-fous structurés.

> **Principe (AX)** : les FAITS calculés (âge data, session, etc.) sont **injectés
> par le code** dans le contexte, pas décrits en prose dans le mandat. Le mandat
> reste stratégique et minimal.

## Voir aussi
- [Domain tools](agent-tools.md) · [reporting](reporting.md) (`decision_reason` compat, vocabulaire canonique dans `domain`) · Architecture §9.3.
