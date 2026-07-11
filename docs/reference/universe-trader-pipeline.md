# Pipeline agentique Univers → Trader (référence)

État livré au 2026-07-11. Design d'origine :
`docs/superpowers/specs/2026-07-11-universe-mandate-consolidation-design.md`.

Ce document décrit la chaîne de décision **telle qu'implémentée** et les **gardes**
qui empêchent les bugs corrigés de revenir. Le principe directeur : **le LLM digère
l'information non structurée et formule des vues ; le code déterministe transforme
l'intention en chiffres.** L'Univers émet des postures/mandats, jamais d'ordre ni de
quantité ; le trader garde plans, timing, tailles, stops.

## 1. Les cinq artefacts (faits vs décision)

```
GlobalSituationDigest   (FAIT macro global, 1×)      trader/domain/situation/global_digest.py
        ▼
GlobalFamilyBoard       (FAIT comparatif régions)    trader/domain/universe/global_family_board.py
        ▼
GlobalUniversePosture   (DÉCISION région/famille,1×) trader/domain/universe/global_posture.py
        ▼   (injectée identique dans TW/EU/US)
RegionalUniverseMandate (hotlist + mandat/symbole)   trader/domain/universe/mandate.py
        ▼
Trader                  (plans, timing, exécution)   trader/agent/protocol/prompts.py
        ▼
Policy déterministe + RiskGate (tailles, stops)
```

**Invariant d'ordre** : le board et le digest sont des *faits* ; la posture globale
est une *décision* produite **une seule fois** puis injectée identique dans chaque
passe régionale. Aucune passe ne lit la décision d'une autre → pas de dépendance à
l'ordre d'exécution des venues.

Orchestration : `trader/runtime/universe_intelligence_runtime.py`
(`tick_universe_intelligence` → `_prepare_global_situation_digest` →
`_prepare_global_family_board` → `_prepare_global_universe_posture` → boucle venues).

## 2. Les deux niveaux de l'agent Univers

Même rôle, deux portées (pas un 3ᵉ agent PM) :

| | Global (`LlmGlobalPostureAgent`) | Régional (`LlmUniverseAgent`) |
|---|---|---|
| Fichier | `trader/agent/universe/global_posture_agent.py` | `trader/agent/universe/agent.py` |
| Contexte | board + digest + sticky global + venues | candidats venue + brief régional + `family_snapshot` + micro compact + board + digest + **posture globale** |
| Grain | familles × venues | symboles de la venue |
| Micro | ❌ | ✅ compact (+ pull, §4) |
| Sortie | `GlobalUniversePosture` (venue_posture, family_priority, gross/net) | hotlist + `symbol_mandates` |

Les deux réutilisent le même transport (`build_universe_router_from_env`).

## 3. Contrat de mandat par symbole (ce que reçoit le trader)

Écrit par `_build_prepared_mandate`, lu via `UniverseMandateStore.active_slice_for_symbol`,
injecté sous `universe_mandate` dans les faits du symbole (`planner_batch.build_symbol_facts`).
Champs (`SymbolMandate`) : `why_selected`, `role`, `posture`, `directional_view`,
`allowed_sides`, `family_context` (**posture famille décidée** + snapshot), `portfolio_context`
(enveloppe d'exposition advisory), `company_brief_ref`, `confidence`. Niveau venue
(`UniverseMandate`) : `family_postures`, `portfolio_posture`.

Le mandat est **advisory** : jamais `qty/stop/sizing/risk_pct` (rejetés au parsing).
Le trader peut le contredire si la structure locale ne confirme pas.

## 4. Flux micro : compact push + exact pull

- **Push** : projection compacte par candidat, bornée par
  `config/company_intelligence.yaml` (`summary_chars_per_symbol`, `max_points_per_symbol`)
  via `CompanyContextProjectionLimits` (`trader/runtime/company_context_config.py`).
- **Pull** : outil `get_company_briefs(symbols, sections, max_chars)`
  (`trader/agent/universe/tools.py`) dans une boucle multi-tours
  (`compose_with_tool_loop`, `trader/agent/universe/tool_loop.py`) réutilisant
  `execute_tool_round`. **Comportement par défaut** de l'agent univers (self-limiting :
  un seul tour si l'agent ne demande aucun brief), borné par le backstop interne.
- **Trader** : reçoit `company_intelligence` (micro frais, source unique) + le mandat
  (jugement). Le `company_context` figé est **retiré** du mandat injecté au trader
  (dédup) — `load_trader_research_context`.

## 5. Gardes anti-régression (bugs corrigés)

| Bug (avant) | Garde (maintenant) | Test |
|---|---|---|
| Garde de contrat rejetait `universe.v2` (0 mandat sur 441 décisions) | `agent.py` + `tool_loop.py` acceptent `v1`/`v2`, ne rejettent que `legacy.add_remove.v1` | `test_universe_agent.py` |
| `company_thesis.summary` = dict `str()`-ifié (91 % des briefs) | `CompanyThesis.from_mapping` extrait `.point` ; prompt `company_micro` définit le schéma `company_thesis` | `test_intelligence.py` |
| Caps de projection ignorés (600 au lieu de 240) | Lus depuis la config via `CompanyContextProjectionLimits` | `test_company_context_config.py` |
| `drivers == catalysts` (pillars vide) | `drivers` = pillars uniquement | `test_company_context.py` |
| `family_postures` orpheline ; `family_context` = snapshot quanti | branchées dans le mandat (`family_context.posture` = posture décidée) | `test_universe_mandate.py` |
| Double injection micro au trader | `company_context` retiré du mandat injecté | `test_mandate_context_hygiene.py` |

Les productions globales (digest, posture) sont **fail-open** : une erreur agent/persistance
n'interrompt pas le cycle (artefact vide + statut observable).

## 6. Activation

**Aucun flag d'activation** : tous ces comportements sont actifs par défaut — c'est le
comportement du système, pas une option. Le tool loop micro est borné par un **backstop
interne** (`_TOOL_LOOP_BACKSTOP = 8` dans `tool_loop.py`) — un fusible anti-boucle, pas
un réglage. La posture globale est produite à chaque cycle (fail-open). Seuls subsistent
des kill-switches d'urgence hérités, tous **ON par défaut**
(`CASYS_UNIVERSE_INTELLIGENCE_ENABLED`, `CASYS_NEWS_MACRO_GLOBAL_ENABLED`).
