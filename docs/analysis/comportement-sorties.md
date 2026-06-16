# Comprendre le comportement des SORTIES — document de travail

> Statut : **en cours** (2026-06-16). Co-analyse Claude (synthèse/data) + Codex
> (backend/code). But : comprendre finement comment les positions se ferment,
> pourquoi le hard_stop tue, et où brancher un feedback loop. Pas encore de
> décision d'implémentation — on cherche d'abord à comprendre.

## 1. Les trois chemins de sortie

| Chemin | Déclencheur | `intent` | `exit_reason` | Statut tagging |
|---|---|---|---|---|
| **Auto / exit_engine** | stop/TP/trailing/max_hold atteints | `PLANNED_EXIT` | `hard_stop`, `trailing_stop`, `take_profit:tpN`, `max_hold` | ✅ propre |
| **LLM / manuel** | le LLM décide de sortir | `CLOSE`/`REDUCE`/`REVERSE` | `llm_exit` … ou **rien** | ⚠️ incohérent |
| **Armé / resolve** | veille armée déclenchée | (selon ordre) | résolu via `resolve_exit_plan` (D11) | ✅ (armé seulement) |

## 2. Le « close » bizarre = sorties LLM non taguées  *(confirmé, data)*

`state/model_performance.jsonl` (67 lignes) — sorties `intent=CLOSE` (14) :
- **5** ont `exit_reason="llm_exit"` ✅
- **9** ont `exit_reason=None` → `attribution.py:181` retombe sur l'intent brut
  → s'affichent **« close »** dans `by_exit_reason`.

`attribution.py:181` : `"exit_reason": row.get("exit_reason") or str(row.get("intent") or "")`
`daemon.py:78` : `_llm_exit_reason_for_intent` ne tague `llm_exit` que pour `{CLOSE, REDUCE, REVERSE}`.

➡️ **Conséquence** : les sorties LLM sont éclatées en 2 catégories (`llm_exit` +
`close`) dans les stats → l'analyse de perf par raison de sortie est faussée.

**Cause (Codex)** : le tag `llm_exit` a été **introduit le 15/06 ~16:11 +0800**
(commit `17bfb62` « Show closed trade exit reasons in cockpit »). Les sorties LLM
**antérieures** n'ont pas le tag → fallback intent → « close ». C'est donc un
artefact **temporel** (avant/après le commit), pas deux chemins concurrents.
➡️ Les « close » sont des sorties LLM légitimes mal étiquetées par ancienneté.

## 3. Le hard_stop = le plus gros tueur de P&L  *(priorité)*

⚠️ **Correction (fact-check Codex sur l'état local)** : sur les 3 « pires », seuls
**CFR.SW (−47 €) et ASML.AS (−49 €)** sont des `hard_stop`. **DBK.DE n'est PAS un
hard_stop** : c'est un `CLOSE` tagué `llm_exit` — le LLM a coupé volontairement
« avant que l'invalidation parte en hard_stop » (stop 29.45 / entrée 29.955 =
~2.5× vol, sain). Donc le LLM sait parfois bien couper avant le stop.

CFR/ASML = **régime armé pré-D11** : prix absolu **figé pré-open**, devenu trop
serré au tir (CFR **0.84× vol**, ASML **1.26× vol**, cf post-mortem). Corrigé
par D11 pour l'armé.

**Mécanique du hard_stop (Codex, Q5)** — pourquoi il fait mal :
- **Prioritaire & stop-first** : dans `exit_engine.evaluate_plan` l'ordre est
  `hard_stop > max_hold > TP > profit_protection > trailing`
  (`exit_engine.py:296`). Si stop et TP se croisent dans la même barre, **le stop
  gagne**.
- **Exécution conservatrice** : si le prix courant est déjà pire que le stop, le
  **fill est pire que le stop** (`exit_engine.py:32,305`).
- **Direct** : prix absolu du LLM, **aucun recalibrage** (`daemon.py:1947`,
  `trade_plan.py:723`). **Armé post-D11** : `resolve_exit_plan` recalibre avec
  clamp min/max (`trade_plan.py:415+`).

⚠️ **Limite du levier §4** : unifier direct→`resolve_exit_plan` débloque les
stops relatifs/structurels **et le clamp** en direct — mais **ne corrige pas un
stop absolu (`type:"price"`) trop serré choisi par le LLM**. Pour ça : forcer/
recommander le relatif en direct, ou un floor de distance.

**Contribution P&L par `exit_reason`** (round-trips `attribution`, *devises
locales non converties — ordre de grandeur*) :

| exit_reason | n | P&L |
|---|---|---|
| **`hard_stop`** | 7 | **−402** 🔴 |
| `llm_exit` | 5 | −44 |
| `trailing_stop` | 6 | −43 |
| `max_hold` | 3 | +44 |
| `CLOSE` (non tagué) | 9 | +50 |
| `take_profit:tp1/tp2` | 4 | **+385** 🟢 |

➡️ **Le hard_stop est le tueur absolu**, les TP portent tout le P&L. Les sorties
LLM (`close`+`llm_exit`) sont ~neutres → le LLM coupe correctement.

### Décomposition des 7 hard_stops (reconverti EUR — les sommes brutes mêlaient pence/USD/CHF)

| catégorie | trades | ~EUR | statut |
|---|---|---|---|
| **Pré-D11** | CL=F −93, CFR −47, ASML −49 | **−189** | corrigé par D11 (armé) |
| **Post-D11 ARMÉS** | AZN −0,4, BP −1,2 | **−1,6** | ✅ **D11 marche** (≈ break-even) |
| **Post-D11 DIRECTS** | ABBV −31, AVGO −39 | **−70** | ❌ **trou à corriger** |

**Conclusions fortes :**
- D11 (stop relatif résolu au tir) **fonctionne** : les armés post-D11 sont ≈ BE.
- **100% du résiduel post-D11 vient des ENTRÉES DIRECTES** (pas de resolver →
  stop absolu serré). CL=F (le pire, −93€) était aussi un short direct.
- ➡️ **Le levier « unifier direct/armé via `resolve_exit_plan` » cible
  exactement la seule catégorie non corrigée.** Priorité d'implémentation #1.

⚠️ *Piège méthodo : les P&L bruts d'`attribution` sont en devise locale (BP.L
« −99 » = −99 pence ≈ −1,2€). Toujours reconvertir avant d'additionner.*

## 4. Stop des entrées DIRECTES : le trou D11  *(✅ CORRIGÉ — commit `a5c04df`)*

> **État initial (avant `a5c04df`)** ci-dessous, conservé pour l'historique.
> Depuis, les entrées directes passent par `resolve_exit_plan` (cf « Correction
> livrée » en fin de section).

- Chemin **armé** : `resolve_exit_plan` (`daemon.py:1562`) résout le stop relatif
  (`volatility_multiple`/`structural`) sur vol fraîche → AZN/BP/AMAT = ✅ break-even.
- Chemin **direct** (`daemon.py:1874/1916`) : `_hard_stop_price` (`l.345`)
  **renvoie None si `type != "price"`** → un stop relatif est **jeté**. Seul un
  prix absolu est retenu.

➡️ Les entrées directes (`OPEN_SHORT`/`OPEN_LONG`) ne bénéficient **pas** du
resolver D11. Le contrat LLM (`codex_client.py:299`) impose d'ailleurs un
hard_stop immédiat **en nombre ou `{type:"price"}` seulement** ; les formes
relatives ne sont documentées que pour les plans armés `EXECUTE_ORDER` (`:375`).

**Incohérence de vocabulaire (Codex, Q4)** : en direct, un **`trailing_stop`
`volatility_multiple` EST accepté** (si `reference_volatility` dispo,
`trade_plan.py:319`) alors qu'un **`hard_stop` `volatility_multiple` est REJETÉ**
(`trade_plan.py:286`). Même mot, deux comportements.

**Dette REVERSE (Codex)** : `REVERSE` n'est pas clampé au risque et l'absence de
hard stop ne le bloque pas (contrairement à OPEN_LONG/SHORT) — `daemon.py:1918,1934`.

**✅ Correction livrée (`a5c04df`)** : sur une entrée directe `OPEN_LONG/SHORT`
(hors armé), le `exit_plan` est désormais routé par le **même `resolve_exit_plan`**
que l'armé (vol fraîche au prix d'entrée + barres), avant le check risque/sizing,
puis persisté résolu. Débloque `percent`/`volatility_multiple`/`structural` + TP
en `risk_multiple` en direct. Échec de résolution → `reason=invalid_exit_plan:<code>`
(pas de fallback, pas de réveil). L'incohérence vocabulaire (trailing vs
hard_stop) et la dette REVERSE restent **hors scope** (à traiter séparément).
**Limite** : ne corrige pas un `type:"price"` trop serré choisi par le LLM
(cf §3) → étape 2 = inciter le LLM au `structural` via le prompt.

## 5. Feedback loop : capturer POURQUOI le LLM sort  *(idée Erwan)*

Aujourd'hui une sortie LLM n'est qu'un label (`llm_exit`/`close`) — le
**raisonnement** du LLM (« ce qu'il a vu ») n'est pas exploité.

**Où il vit / meurt (Codex, Q6)** : `Decision.rationale` est bien capturé et
**persisté dans `decisions.jsonl`** (via `record_decision` → `decision_ledger`).
**MAIS** la ligne de sortie écrite dans `model_performance.jsonl`
(`daemon.py:2052`) **ne contient pas `rationale`**, et `compute_round_trips` ne
lit **que** `model_performance.jsonl` (`attribution.py:121`) → le « pourquoi » de
sortie est **invisible pour l'attribution/feedback**.

**Faisabilité (simple)** : propager `exit_rationale = decision.rationale` dans
`_append_model_performance` pour `CLOSE`/`REDUCE`/`REVERSE`, puis le recopier dans
les round-trips. Variante robuste : ajouter un `decision_id` reliant à
`decisions.jsonl`. → l'idée d'Erwan est directement réalisable.

**Bonus (Codex, Q3)** : `max_hold` **est bien imposé** (`exit_engine.py:104,310`,
priorité après hard_stop) — les 3 `max_hold` viennent de plans persistés avec
`max_hold_minutes`, pas d'un artefact. (Corrige la note « max_hold non imposé ».)

## 5bis. Hypothèse-racine n°1 : profil scalping vs données différées  *(découvert 16/06)*

En traçant un trade perdant en live (AMAT 16/06, long stoppé à −1.9 % en 15 min),
on remonte à une **incohérence dans le mandat/stratégie**, pas un bug ni un biais
LLM :

- `mandate.md:51-55` : la **fraîcheur est documentée** — `data_age_m` exposé, le
  LLM doit justifier qu'il tolère le différé. Il l'a fait (« data_age 14m
  acceptable »).
- `memory.md:14` : le profil = **« scalping gentil / momentum intraday court sur
  barres horaires »**.
- `daemon.py:86` : `DEFAULT_RUNTIME_INTERVAL = "15m"` (« pour coller à la cadence
  scalping »).

➡️ **Contradiction structurelle** : on demande du **momentum intraday court**
sur des données **différées de ~14 min**. Sur un breakout 15m, le temps que le
signal parvienne au LLM et qu'il exécute, **le mouvement est déjà consommé** →
il entre mécaniquement **au sommet** (AMAT acheté à 600.5 = high du jour 600.91,
reversal immédiat). Même signature que CFR/ASML. Le LLM exécute fidèlement deux
consignes incompatibles ; ce n'est pas lui le problème.

**Le pattern « chase the breakout » est un symptôme du différé, pas (que) un
biais** : la latence transforme une stratégie momentum en achat-au-sommet.

### Carte d'impact d'un passage scalping → swing (estimé léger)
- **Déjà swing** (rien à faire) : sélection radar/rotation (daily, dwell 3j),
  cockpit daily, architecture stops/plans/exit_engine (agnostique à l'horizon).
- **À changer (peu, mais à recalibrer)** : `DEFAULT_RUNTIME_INTERVAL` 15m→1h/4h,
  cadence de réveil, `DEFAULT_MAX_MARKET_DATA_AGE_MINUTES`, le profil `memory.md`,
  les distances de stop (vol daily plus large). `max_hold` est déjà par-plan.
- **Coût réel** = re-calibration + re-validation (changement de *nature*), pas
  une refonte de code.

**À tester avant d'agir** : comparer le P&L des round-trips **par horizon de
détention** (les courts perdent-ils, les longs gagnent-ils ?) pour valider
empiriquement l'hypothèse latence/horizon.

## 6. Pistes de correction (à arbitrer APRÈS compréhension)
- Normaliser le tagging des sorties LLM (plus de « close » fantôme).
- Unifier la résolution de stop direct ↔ armé (resolver D11 partout).
- Réviser le **serrage** du hard_stop (la vraie cause des grosses pertes).
- Persister le rationale de sortie → boucle de feedback.

---
*Sections [CODEX …] alimentées par l'investigation `investigate-exits` en cours.*
