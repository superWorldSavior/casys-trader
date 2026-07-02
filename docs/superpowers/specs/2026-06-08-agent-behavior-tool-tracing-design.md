# Traçage du comportement de l'agent — outils utilisés & choix

**Date** : 2026-06-08
**Statut** : **LIVRÉ** 2026-07-02 — 3 couches implémentées et testées : données (context_request args + next_wake effectif), dérivation (summarize_tools), jugement (CLI tool_usage × score forward)

---

## 1. Contexte & problème

On veut **observer ce que fait l'agent**, pas seulement sa décision finale : quels
« outils » il mobilise, quand, et les **choix qu'il fait ou ne fait pas**. Aujourd'hui
le traçage est **partiel et éparpillé** :

- `state/decisions.jsonl` (decision_ledger) : action, intent, qty, confidence,
  rationale, `next_wake_in_minutes`, `indicator_watch_created`/`_requested`/
  `_rejections`, `learning`, provider/model/fallback.
- `state/events.jsonl` : `cycle_started`, `cycle_completed`, `decision_recorded`,
  et `context_resolved` (mais **seulement des compteurs** : `requested=N, resolved=M`,
  cf `trader/daemon.py:545`).

Manques :
- **Quels** indicateurs/symboles/timeframes l'agent a demandés (pas juste combien).
- Une **vue consolidée par décision** de la séquence d'outils (la boucle
  demande→fetch→redécision n'est pas reconstituable proprement).
- Un signal exploitable sur le **non-usage** d'un levier (l'agent **ne pull pas** —
  cf [[casys-trader-finding-watch-levers]] et le design cockpit v2). Le non-usage
  est aujourd'hui invisible, masqué.

**Deux intentions, validées** :
1. **Observer / auditer** — avoir une trace machine-readable des « workflows » d'outils
   par décision (le quoi, factuel).
2. **Juger l'interface** — savoir si le sous-usage est *justifié* (l'outil n'apporte
   rien dans ce contexte) ou s'il révèle une **friction d'interface** (l'outil est mal
   exposé / trop coûteux à invoquer). C'est une question AX, pas une question d'agent.

**Contrainte produit explicite (Erwan)** : *ne pas obliger l'agent à écrire son
raisonnement*. On trace les **outils** et les **choix**, pas la chaîne de pensée. Le
« justifié ou pas » se déduit **a posteriori par corrélation**, jamais en interrogeant
l'agent (cf §5).

## 2. Modèle d'« outils » de l'agent (état réel)

L'appel de décision est **pur** (`codex_client`, `--allowed-tools ""`, terminal
coupé) : pas de function-calling inline. Les outils sont des **canaux de sortie
structurée**, exécutés par le daemon (modèle requête/réponse) :

| Outil (canal)            | Champ JSON              | Exécuteur / effet daemon                          | Déjà dans la row ? |
|--------------------------|-------------------------|---------------------------------------------------|--------------------|
| Demande de contexte      | `requests`              | `resolve_indicator_requests` → re-prompt (≤2 tours)| ❌ compteur seul (event) |
| Veille conditionnelle    | `indicator_watch`       | `build_indicator_watch` → scheduler               | ✅ `runtime.indicator_watch_*` |
| Auto-planification       | `next_wake_in_minutes`  | `set_symbol_next_wake` (borné)                    | ⚠️ demandé seul, pas l'effectif |
| Ordre                    | `action/quantity/intent`| RiskGate → SimBroker/IB                            | ✅ `action/intent/executed/reason` |
| Apprentissage            | `learning`              | consolidation mémoire                             | ✅ `learning` |

Constat clé : **4 des 5 canaux sont déjà encodés dans `build_decision_row`**
(`decision_ledger.py:85-123`). Il n'y a que **2 vrais trous de données** :
1. les **args du `context_request`** (symbol / indicateurs / timeframe demandés — dispo
   dans `req.requests` à `daemon.py:545`, jamais redescendus dans la row) ;
2. le **`next_wake` effectif/borné** (le clamp de `set_symbol_next_wake`).

Tout le reste est de la **présentation**, pas de la donnée manquante.

## 3. Objectif

Produire, **par décision**, une **trace d'outils** machine-readable et déterministe
(*quels outils, avec quoi, qu'en a fait le daemon*), **dérivée d'une source unique** —
sans forcer de rationale — et la rendre **joignable au score qualité forward** pour que
le « sous-usage justifié ou non » émerge des données.

## 4. Proposition (validée) — 3 couches

### 4.1 Couche DONNÉE — combler les 2 trous (le seul vrai boulot)

On n'ajoute **pas** un blob redondant : on ajoute à la row les 2 primitives manquantes.

- **`context_request` args** : threader `req.requests` (symbol/indicateurs/timeframe) +
  `resolved=N` depuis la boucle `decide_all_due_symbols` (`daemon.py:~528-568`) jusqu'au
  call-site de `build_decision_row`. Piste : accrocher le résumé de résolution sur le
  `Decision` au 2e batch. **C'est la question ouverte d'implémentation.**
- **`next_wake` effectif** : capter la valeur bornée de `set_symbol_next_wake` à côté de
  la valeur demandée déjà présente (`next_wake_in_minutes`).

### 4.2 Couche DÉRIVATION — `summarize_tools(row) -> trace`

**Pas de `tool_trace[]` stocké.** Une **fonction pure** dérive la trace depuis les champs
de la row. La fonction *est* le schéma. Avantages AX : déterministe, single source of
truth (zéro drift entre « l'ordre a-t-il exécuté » top-level et un blob dupliqué),
narrow, test-first par outcome.

Forme retournée (calculée, jamais persistée en double) :

```json
{
  "tools_used":   ["next_wake", "order"],
  "tools_skipped":["context_request", "indicator_watch", "learning"],
  "rounds": 1,
  "trace": [
    {"tool": "context_request", "invoked": false},
    {"tool": "next_wake", "invoked": true,
     "args": {"requested_minutes": 90}, "outcome": "clamped",
     "detail": {"effective_minutes": 60}},
    {"tool": "order", "invoked": true,
     "args": {"action": "BUY", "intent": "OPEN_LONG"}, "outcome": "executed"}
  ]
}
```

`outcome` = enum fermé : `resolved | created | rejected | applied | clamped | executed |
blocked | noop`. Le **non-usage** (§le cœur du finding) sort naturellement dans
`tools_skipped` + `invoked: false`, sans entrée bruyante imposée.

### 4.3 Couche JUGEMENT — joindre trace × score qualité

Le CLI lecture-seule `python -m trader.tool_usage` (miroir de `stats`/`attribution`) ne
sort **pas que** des taux d'usage. Il **joint la trace au score qualité forward**
(`d632cbe`) sur `decision_id` pour répondre à *le sous-usage coûte-t-il ?* :

- décisions **sans pull** ≈ aussi bonnes que **avec pull** → sous-usage **justifié**.
- décisions **sans pull** systématiquement moins bonnes → outil **sous-exploité** =
  signal de friction d'interface à corriger.

Sorties : taux d'usage par outil, distribution des indicateurs demandés, % de décisions
où l'agent a pull, watch posées/rejetées, wakes bornés, ordres bloqués par raison —
**croisés** avec le score forward par bucket.

### 4.4 Hypothèse de friction déjà visible (à valider par la couche 3)

`codex_client.py:167` présente `next_wake` et `indicator_watch` comme **alternatives**
(*« soit fixer `next_wake_in_minutes`, soit poser une `indicator_watch` »*), avec le
moins cher en premier : `next_wake` = un nombre, `indicator_watch` = un objet multi-champ
(`timeframe`/conditions/`ttl_minutes`/`on_trigger`). Asymétrie de coût cognitif → l'agent
prend le moins cher. Le sous-usage de la watch pourrait être un **artefact de
présentation**, pas un choix (cf [[casys-trader-finding-watch-levers]]). La couche 3
permet de trancher sans rien demander à l'agent.

## 5. Explicitement HORS périmètre

- **Demander à l'agent de justifier l'usage/non-usage d'un outil.** Trois raisons :
  (a) ce serait le CoT forcé qu'on s'interdit (§1, invariant §7 *narrow*) ;
  (b) l'observateur biaiserait l'observé — demander « pourquoi pas de watch » pousse à en
  poser, polluant le signal même qu'on mesure ;
  (c) le self-report d'un LLM est rationalisé a posteriori, sans valeur causale.
  → Le « justifié » se déduit **par corrélation forward**, pas par interrogation.
- Forcer/loguer la **chaîne de raisonnement** (rationale reste optionnel, inchangé).
- Tracer les **résultats complets** des indicateurs fournis (volumineux) — on garde un
  résumé (`resolved=N`, noms), pas les séries.

## 6. Questions ouvertes (réduites)

Tranché en discussion : Q-granularité → **agrégé par décision + compteur de tours**
(§4.2 `rounds`). Q-stockage → **dérivé, pas de fichier séparé ni de blob inline
redondant** (§4.2). Q-redondance ordres → **résolue par la dérivation** (la fonction lit
`executed`/`reason`, ne les duplique pas).

Restent :
1. **Capture des args de `requests`** (§4.1) : où threader proprement `req.requests` +
   `resolved` depuis `decide_all_due_symbols` jusqu'à `build_decision_row` ? C'est le
   vrai travail d'implémentation.
2. **Surfaçage** : CLI seul d'abord, ou aussi exposé au cockpit/TUI ? (penche CLI seul.)

## 7. Invariants (AX)

- **Déterministe** : mêmes décisions → même trace (fonction pure sur la row).
- **Machine-readable** : `outcome` = enum fermé, pas de prose.
- **Single source of truth** : la trace est dérivée, jamais persistée en double.
- **Narrow** : la trace décrit *ce qui s'est passé*, pas *pourquoi* (pas de CoT).
- **Test-first** : chaque `outcome` a un test (création, rejet, clamp, blocage, noop) +
  un test de jointure trace × score.

---

**Prochaine étape** : exécution en pair-codex (Opus orchestre, Codex tape), TDD. Ordre :
(1) couche donnée — combler les 2 trous, capture `requests` ; (2) `summarize_tools` pur,
test-first par outcome ; (3) CLI `tool_usage` avec jointure score forward.
