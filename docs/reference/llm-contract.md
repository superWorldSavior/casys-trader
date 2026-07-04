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
9. `# Contrat de sortie` — `_batch_compact_contract()` si `allow_context_request`
   (l'agent peut demander plus de contexte), sinon `_batch_final_contract()`.

Enums injectés (source de vérité côté code) : indicateurs de veille
(`DEFAULT_INDICATORS`), opérateurs (`WATCH_VALID_OPERATORS`), codes de raison
(`decision_reason.reason_code_enum_text()`).

## Le contrat de sortie

JSON strict par symbole : `action`, `quantity`, `confidence`, `intent`, `rationale`,
`exit_plan` (hard_stop **fortement recommandé, PAS obligatoire** — l'agent choisit
son niveau), `next_wake_in_minutes`, `indicator_watch` (veille/plan armé),
`cancel_watch_ids`, `learning` (note apprise, `string|null`), `decision_reason_code`.

**Alternative** : `action: "REQUEST_CONTEXT"` — un objet entier (pas un champ) qui
retourne une `ContextResearchRequest` pour demander plus d'indicateurs.

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
- [Domain tools](agent-tools.md) · [reporting](reporting.md) (`decision_reason`) · Architecture §9.3.
