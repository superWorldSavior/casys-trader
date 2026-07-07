# Feedback loop sur les action tools + audit de propreté (tools & architecture)

**Date** : 2026-07-07
**Statut** : 🔍 **ANALYSE / CADRAGE** — aucune implémentation. Ce document archive
les résultats de quatre analyses (3 explorations de code fan-out + 1 audit
architecture) menées le 2026-07-07, pour **ne pas perdre les insights** et cadrer
un futur chantier. Rien n'est décidé ni livré ici.
**Origine** : incident APD (MAJ d'exit rejetée hors-séance,
`resolve_failed:hard_stop_bars_unavailable`) → question d'Erwan : « prévenir
l'agent quand un outil échoue, c'est un harness classique ; on ne le fait pas
parce qu'on a un MCP-like qui n'en est pas, non ? » → audit de propreté avant
d'envisager le chantier.

> **Portée** : (1) constater pourquoi les **action tools** (outils qui *écrivent*
> l'état) n'ont aujourd'hui aucun retour vers le LLM, (2) évaluer si l'architecture
> des outils et l'arborescence sont **propres** avant d'y toucher, (3) identifier
> les **points de branchement** exacts d'un futur feedback loop et son pré-requis.

---

## 0. Résumé exécutif (verdict)

| Axe | Verdict | Détail |
|---|---|---|
| Parsing / compilation Pine-like | ✅ propre | frontière claire, `Decision` frozen, fail-safe HOLD |
| Context tools (read-only, tool-round) | ✅ exemplaire | `validate→execute→result typé→réinjection`, boucle bornée. **Le modèle à suivre** |
| Application des **action tools** | ⚠️ dette | pas de type de retour ; outcome reconstruit *a posteriori* via dict `entry` muté ; validation *dans le daemon*, *après* `decide_one` |
| Layering / arborescence | ✅ globalement sain | `domain`+`support` = puits purs, refactor « boundaries » actif. 2 dettes structurelles |
| `daemon.py` | ⚠️ god object | **2660 lignes** ; `_execute_one_cycle_decision` ~480 lignes |
| Types de domaine | ⚠️ mal placés | `TradePlan`/`Fill`/`Order`/`Position` hors de `domain/` → inversion `infra→planning/execution` |

**Conclusion** : socle sain, deux dettes qui **convergent** vers une même prochaine
tranche de refactor — *extraire l'application des actions hors du daemon avec un
type de retour structuré*. Cette tranche assainit le god object **et** débloque le
feedback loop.

---

## 1. Pourquoi il n'y a aucun feedback (les deux pipelines)

Le système a **deux familles d'outils, sur des rails séparés**, de qualité
asymétrique.

### 1a. Context tools (lecture) — vrai tool-use, avec boucle ✅

- Le LLM demande une tournée en renvoyant `{"tool_calls": [...]}` au lieu de
  `{"decisions": [...]}` → parsé en `BatchToolCallRequest`
  (`parsing.py:612-626`, type `protocol/types.py:70-80`).
- Catalogue = **7 outils read-only** injectés au prompt (`prompts.py:525-542`) :
  `get_freshness`, `get_active_plans`, `get_attribution`, `describe_data`,
  `find_indicators`, `get_indicator_context`, `recall_learnings`.
- Exécution : `tool_round.run_one_round` → `agent_tools.execute_tool_round`
  (`agent/tools/core.py:256-318`) → chaque call rend un `(AgentToolResult,
  AgentToolTrace)` **typé** avec outcome ∈ {ok, rejected, error, budget_exhausted,
  truncated}. Budget borné (24 total, 3/8 par symbole).
- **Réinjection** : `resolve_symbol_decision` (`tool_round.py:157-189`) reboucle —
  `per_symbol = {**base_facts, "tool_results": this_round_results}` (mode `delta`)
  → renvoyé au LLM au round suivant. Boucle bornée par
  `SESSION_ROUND_BACKSTOP=20` (`decide_one.py:47`).

### 1b. Action tools (écriture) — contrat déclaratif, sans retour ⚠️

- Émis dans la réponse **finale** sous la clé `calls` de chaque `decisions[n]`,
  compilés par `compile_strategy_call` (`protocol/strategy_language.py:115-144`) :
  `strategy_entry`→`position_order`, `strategy_exit`→`exit_rule`,
  `strategy_close`→`position_order`, + `set_next_wake`, `propose_indicator_watch`,
  `cancel_watch`, `record_learning`. Dispatch dans
  `_decision_from_symbol_calls` (`parsing.py:309-499`).
- **Appliqués APRÈS `decide_one`**, dans le daemon (`_execute_one_cycle_decision`,
  `daemon.py:970`) : résolution position-aware, validation exit, risk gates,
  exécution, plan_store.
- L'outcome (`applied`/`rejected`/`resolve_failed:…`) n'est **pas** une valeur de
  retour : il est **muté dans un dict `entry`** par `exit_update.py:29-72`, puis
  **relu** par `finalize_action_tool_outcomes` (`tool_outcomes.py:70-90`) pour
  produire la trace finale.
- **Aucun retour au LLM** : le tour se termine à `decide_one` qui retourne
  `(decision, calls_made)` (`decide_one.py:289`). Le seul feedback est
  **asynchrone, au cycle suivant** (learnings + `last_llm_review` réinjectés dans
  les facts). → confirme le constat d'Erwan : « MCP-like qui n'en est pas » pour
  les actions.

**Destinations de l'outcome** (consommateurs post-hoc) : `agent_trace.log`
(`[agent-tool]`), `decisions.jsonl` (`runtime.tool_calls` + champs plats
`exit_update_*`), read-model cockpit (`reporting/read_models/tool_usage.py`,
`interfaces/cockpit/pages/decisions.py`), `plan_store` (état), `learnings.db`
(recall trace).

---

## 2. Le rail de feedback est réutilisable — le blocage est ailleurs

**Bonne nouvelle** : `tool_results` est un **dict de primitives**
(`{id, tool, ok, result|error}`, `core.py:332-343`). Un rejet d'action aurait le
format **exact** d'un context-tool échoué, et le LLM lit déjà `ok`/`error`. Le rail
n'est pas couplé au cas « lecture ».

**Points de branchement identifiés** (pour le futur chantier) :
- **A** — ajouter `validate_action` (callable) à la signature de
  `resolve_symbol_decision` (`tool_round.py:102-112`), sur le même pattern
  d'injection que `call_model`/`heartbeat`.
- **B/C** — valider la décision avant `_finalize` : à l'early-return
  (`tool_round.py:160-161`, LLM décide sans tools) et après le tour final
  (`tool_round.py:182-189`). Si rejets ET `rounds_done < max_rounds`, construire
  `per_symbol = {**base_facts, "tool_results": rejection_items}` et reboucler.
- **D** — injecter le validateur depuis `decide_one` (`decide_one.py:225-234`),
  construit à partir des services métier.
- **E** — ajouter `action_validator` à `ToolRoundServices` (`decide_one.py:53-75`),
  analogue à `learnings_recall_provider`.

**Le vrai obstacle (pas le rail)** : pour reboucler *dans la session*, il faut
**valider les actions AVANT que `decide_one` retourne**. Or aujourd'hui la
validation est *en aval*, dans le daemon, et dépend de `plan_store`/`bars`
(`exit_update.py`) et des risk-gates (`daemon.py:1209-1294`) — **absents du
`ToolContext`**, et de l'autre côté de la **frontière worker/daemon** (en mode
queue, `decide_one` tourne dans un worker, l'application dans le thread principal).

Donc le chantier n'est pas « brancher un fil » : c'est **extraire la validation des
actions en une fonction pure sans effet de bord (dry-run), injectable**.

---

## 3. Dette & cohérence des tools

**Ce qui est propre** : parsing/compilation (frontière nette, `Decision` frozen,
`compile_strategy_call` pure) ; context tools (archi typée exemplaire) ; migration
Pine-like isolée et idempotente (`strategy_language_migration.py`).

**Ce qui porte de la dette** :
1. **Pas de type `ActionToolResult`** symétrique à `AgentToolResult`. L'outcome est
   reconstruit post-hoc par lecture de champs plats sur `entry` muté. `tool_outcomes.py`
   est un module de *reconstruction*, pas d'*observation directe*.
2. **`apply_exit_update_to_open_plan`** (`exit_update.py:29-72`) fait persistance +
   audit en un appel, sans retour structuré. Le daemon appelle `_apply_exit_update`
   puis `record_decision` séquentiellement, sans valeur intermédiaire
   (`daemon.py:1088-1101`).
3. **Trigramme de noms** pour le même concept : `amend_exit` (G1, mort dans le code,
   vivant dans les données) → `exit_update` (G2, interne) → `strategy_exit` (G3,
   outil LLM). Idem `propose_order`→`strategy_entry`. Compat-lecture dans
   `strategy_language_migration.py:16-21,135`. Chaîne à 4 alias pour tracer un
   `strategy_exit`.
4. **`domain_tools`** (`types.py:28`) = sac `dict|None` non typé (context rounds,
   normalizations, mergés).

**Incohérences mineures à corriger au passage** (non bloquantes) :
- `strategy_exit` sur plan inexistant → outcome **`"noop"`** au lieu de
  `"rejected"` (`tool_outcomes.py:38`, le test `startswith("resolve_failed")` rate
  `"no_open_plan"`).
- `record_learning` → **toujours `"applied"`** (`tool_outcomes.py:27`), non corrélé
  au succès réel du store.
- `get_active_plans` **renvoie les watches, pas les TradePlans**
  (`agent/tools/plans.py:20`) — nom trompeur ; les plans réels ne sont exposés à
  aucun outil LLM.
- **Deux systèmes de traces** parallèles : `reporting/tool_trace.py:9` (legacy
  synthétique depuis `runtime.*`) vs `runtime.tool_calls` (structuré) → double
  comptage d'usage.
- `set_next_wake{when}` compile silencieusement en `indicator_watch{WAKE}`
  (`parsing.py:431-442`) — délibéré et documenté, garde anti-collision
  (`parsing.py:478`), mais un même champ `indicator_watch` a deux origines.

---

## 4. Architecture / arborescence / layering

**Packages** (nb fichiers) : `application` 35, `interfaces` 32, `market` 30,
`reporting` 26, `agent` 24, `runtime` 19, `infrastructure` 17, `support` 8,
`planning` 6, `execution` 6, `domain` 6.

**Layering globalement respecté** : `domain` et `support` = **puits purs** (0 import
sortant) ✓ ; `runtime` = compositor top ✓ ; aucun cycle au niveau couche. Refactor
**actif et discipliné** (~30 « boundaries » dans `docs/superpowers/plans/`).

**Dette structurelle #1 — `daemon.py` = 2660 lignes** (god object). Le suivant fait
1653. Malgré les tranches déjà extraites, `_execute_one_cycle_decision`
(~`daemon.py:970-1449`, ~480 lignes) concentre toute l'orchestration — **c'est là
que vit l'application des actions**.

**Dette structurelle #2 — types de domaine mal placés → inversion
`infrastructure → planning/execution`**. La matrice montre `infra` important
`planning` (9×) et `execution` (4×) : `state_db/trade_plan_store.py:30`
(`TradePlan`), `broker_store.py:21-22` (`Fill`/`Order`/`Position`). Cause racine :
ces types sont *de domaine* mais rangés dans `planning`/`execution`, pas `domain/`.
Symptôme : imports **locaux** `# pas de circular dep` (`broker_factory.py:133,185,238`,
`migrations.py:239`) = cycles cassés à la main. Migrer ces types vers `domain/`
**supprimerait** l'inversion (infra→domain est légitime).

**Inversions mineures** : `reporting→application` (`tool_trace.py:7`),
`market→agent` (`market/rotation/{wiring,override}.py`, rotation LLM-assistée).
**Légitimes** : `reporting→interfaces` (main CLI) et `interfaces→runtime`
(`cockpit/supervisor.py` pilote le daemon via `runtime.pid_file`).

**Façades de compat** : 6 `__getattr__` dans `trader/__init__.py` (alias virtuels
`trader.queue.*`→`infrastructure.queue`, etc.). Dette **transitoire assumée** du
strangler ; à gommer en fin de refactor.

---

## 5. Chantier proposé (à décider — non engagé)

Les deux dettes structurelles et le feedback loop pointent vers **une même
tranche** :

### Tranche « action-application-boundary » (pré-requis feedback loop)
1. **Extraire `_execute_one_cycle_decision`** du daemon vers une couche
   `application` dédiée → dégonfle le god object.
2. **Introduire `ActionToolResult`** (ou `ExitUpdateResult(applied, reason, trace)`)
   comme **valeur de retour** de l'application des actions, au lieu de muter `entry`.
   Non-cassant : `entry` peut rester alimenté pour la compat.
3. **Séparer valider / appliquer** : un **validateur dry-run** (sans effet de bord,
   réutilisable en amont) + l'application réelle.

### Chantier « feedback loop » (après le pré-requis)
- Injecter le validateur dry-run via `ToolRoundServices.action_validator` (point E),
  le passer à `resolve_symbol_decision` (A), valider avant `_finalize` (B/C),
  réinjecter les rejets en `tool_results` (format context-tool identique).
- **Couplages sémantiques à traiter** : le prompt `_TOOL_CATALOG`
  (`prompts.py:525-542`) présente `tool_calls` comme read-only → décrire le
  feedback de correction ; l'invariant « `tool_results` = context tool exécuté »
  est rompu par un rejet d'action injecté synthétiquement (acceptable, à documenter).

### Nettoyages annexes (opportunistes, indépendants)
- `no_open_plan` → `"rejected"` ; `record_learning` outcome réel ; renommer/clarifier
  `get_active_plans` ; unifier les deux systèmes de traces ; (plus tard) migrer les
  types de domaine vers `domain/` et gommer les façades `__getattr__`.

---

## 6. Explicitement hors scope / reporté
- L'implémentation (ce doc ne fait que cadrer).
- La migration des types vers `domain/` = tranche séparée, plus large.
- Le fix d'affichage cockpit « exit rejected » (bug de fraîcheur) est **déjà livré**
  (main `7ebd687`) et sans rapport avec ce chantier.
- Issue [#15](https://github.com/Casys-AI/casys-trader/issues/15) (parsing tolérant
  du JSON) = orthogonale.

## 7. Méthode
Analyses menées le 2026-07-07 : 3 explorations de code en fan-out (pipeline action
tools, rail de feedback context tools, dette/cohérence) + 1 audit architecture
(matrice de dépendances inter-couches, tailles de fichiers, façades). Verdicts
convergents. **À fact-checker par Codex avant d'engager le chantier.**
