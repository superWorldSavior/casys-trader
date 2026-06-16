# Architecture — casys-trader

> Ce document décrit l'architecture technique du daemon de trading paper piloté par LLM.
> **Généré par analyse statique du code — à re-vérifier si l'architecture évolue.**

---

## 1. Vue d'ensemble

casys-trader est un **daemon de trading paper** qui tourne en boucle de cycles.
Le LLM n'est **pas** un opérateur continu : il est un **planificateur** qui conçoit des
scénarios d'entrée (plans armés, veilles) que le daemon exécute mécaniquement, et
qui se réveille sur événement pour les ajuster.

Deux boucles se superposent :

| Boucle | Acteur | Cadence | But |
|--------|--------|---------|-----|
| **Boucle 1** | Humain | ad hoc | Mandat, guardrails, universe config |
| **Boucle 2** | Daemon + LLM | cycle (5–240 min) | Décision, exécution, logging |

Le code est le seul à calculer les indicateurs, évaluer les plans, appliquer les
gates. Le LLM reçoit les faits calculés, raisonne, et retourne des artefacts
structurés (décisions JSON, plans, veilles). `daemon.py:1–11`

---

## 2. Diagramme ASCII — flux d'un cycle

```
Scheduler (timer / indicator_watch trigger)
    │
    ▼
run_cycle()                                     [daemon.py:1131]
    │
    ├─ Chargement config (universe.yaml, risk.yaml)
    ├─ Reload univers (rotation_daemon: compose_active_universe) ─── D9/D10
    │
    ├─ Fetch barres marché (15m × 5j)           [daemon.py:1246]
    │    └─ assess_freshness → stale? → backoff exponentiel
    │
    ├─ Fetch barres daily (1y) → cockpit daily
    ├─ Fetch barres 5m (plans ouverts) → exit checks fins
    │
    ├─ _apply_planned_exits()  ──────────────────  chemin sortie AUTO
    │    └─ exit_engine.evaluate_plan(hard_stop|TP|trailing|max_hold|profit_protection)
    │
    ├─ _scan_exit_watches()                       réveil depuis exit_watch
    ├─ _scan_indicator_watches()                  réveil depuis sched watches
    │
    ├─ build_market_cockpit() → shared_context   [agent_context.py]
    │    (cockpit compact, KPIs, attribution, regime_families, learnings)
    │
    ├─ Gate de pertinence (relevance_gate)  ──── D7 étage A
    │    └─ quiet? → HOLD sans LLM (quiet_gate)
    │
    ├─ Plans armés (EXECUTE_ORDER) ─────────────  D7 étage B
    │    └─ resolve_exit_plan() sur vol fraîche → exécution SANS LLM
    │
    ├─ _batch_decide() ─ 1 appel LLM pour tous les symboles dus
    │    └─ codex_client.decide_batch()
    │         ├─ AcpxBackend (acpx --format quiet exec)
    │         └─ round-trip optionnel REQUEST_CONTEXT (indicateurs à la demande)
    │
    └─ Pour chaque décision :
         ├─ validate intent / exit_plan
         ├─ resolve_exit_plan() (direct OPEN_LONG/SHORT) ─── unification D11
         ├─ RiskGate.check_confidence() + RiskGate.check()
         ├─ SimBroker.submit() → fill
         ├─ create_trade_plan() → TradePlanStore
         ├─ record_decision() → decisions.jsonl + model_performance.jsonl
         └─ _apply_decision_schedule() → Scheduler (next_wake, indicator_watch)
```

---

## 3. Le cycle de décision — de bout en bout

### 3.1 Sélection des symboles dus

`daemon.py:301 _select_due_symbols` — en mode normal : `Scheduler.due_symbols()`
sélectionne les symboles dont le `next_wake` est passé. En mode `--once`/`--bootstrap` :
tout l'univers.

L'univers actif est généré par la rotation (D9/D10) à chaque cycle :
`rotation_daemon.py` appelle `compose_active_universe(now)` qui compose
`sticky_all ∪ union(hot-lists des marchés ouverts)` et écrit
`config/universe.yaml` si le contenu change (`daemon.py:1159`).

### 3.2 Chargement des barres & fraîcheur

Barres 15m / 5j (runtime décisionnel) + barres 1j / 1y (cockpit daily).
`market.assess_freshness()` est le garde « marché live » : une dernière barre trop
vieille (> 40 min par défaut, `daemon.py:86`) → `stale_market_data` → le symbole
est exclu du tradable et reçoit un backoff exponentiel (`daemon.py:258`).

Barres 5m (fenêtre 1j) fetched **uniquement** pour les symboles avec un plan ouvert,
pour la détection fine intra-barre des stops/TP (`daemon.py:1069`).

### 3.3 Construction du contexte partagé

`build_market_cockpit()` (`agent_context.py:73`) produit le tableau compact (cols `s`,
`r`, `vol`, `z`, `er`, `ac`, `rs`, `sz`, régime daily, frais `be_ref_bps`/`rtrip_bps`).

Le `base_context` injecté au LLM contient (`daemon.py:1403`) :
- `cockpit` — tableau cross-asset
- `portfolio` — snapshot (holdings, equity, cash, frais estimés)
- `risk_limits` — config `risk.yaml`
- `kpis` — stats live (win%, P&L, etc.)
- `attribution` — P&L par raison de sortie, calibration confidence
- `regime_families` — biais directionnel calculé par famille thématique (D2)
- `learnings` — guardrails + patterns consolidés (D6)
- `stale_market_data` — liste des symboles exclus ce cycle
- `semantic.requestable_indicator_ids` — indicateurs disponibles via REQUEST_CONTEXT

Faits calculés par le code et injectés (principe AX : pas de prose) :
- `data_age_m` (âge réel en min des barres) — `daemon.py:960`
- `session` (état de la séance par place) — `market.session_snapshot()`
- `active_watches` (résumé des veilles actives par symbole)

### 3.4 Gate de pertinence (D7 étage A)

`relevance_gate.symbol_needs_llm()` (`relevance_gate.py:19`) — fonctions pures.
Passe au LLM uniquement si : réveil agent (`agent_wake`), trigger, position ouverte,
régime fort (≥ 70 % de la famille), signal cockpit (`sig`/`stretched`),
ou revue périodique garantie (4 h). Sinon → `quiet_gate` (HOLD sans appel).
`daemon.py:1616`

### 3.5 Plans armés — exécution sans LLM (D7 étage B)

Les `indicator_watch` à `on_trigger: EXECUTE_ORDER` portent un `order` complet
(intent, qty, confidence, exit_plan). Au déclenchement (`daemon.py:1525`) :
1. `resolve_exit_plan()` — résolution late-binding du stop/TP sur vol fraîche (D11)
2. `armed_order_price_coherent()` — vérif que le prix n'a pas déjà franchi le stop
3. Si conflit multi-scénarios même symbole → réveil planificateur, pas d'exécution
4. Si stale ou position déjà ouverte → annulation + réveil planificateur

Le scénario validé crée une `Decision` directement, sans appel LLM.

### 3.6 Appel LLM batch — `_batch_decide()`

Un seul appel `codex_client.decide_batch()` pour tous les symboles dus & frais.
`daemon.py:926`. Le contexte partagé est envoyé une fois (économie D7).

Round-trip `REQUEST_CONTEXT` optionnel : si le LLM demande des indicateurs
supplémentaires (`ContextResearchRequest`), `resolve_indicator_requests()` les calcule
à partir des barres déjà en mémoire et lance un 2e batch sans ré-appeler Codex une
3e fois. Budget : `max_model_calls_per_cycle` (défaut 25). `daemon.py:979`

Transport : `AcpxBackend.complete()` (`llm.py:322`) → `acpx --format quiet --allowed-tools "" --no-terminal exec [prompt]`.
Sessions jetables (isolation/idempotence). Fallback : `OpenAICompatibleBackend` (Ollama) si `TRADER_OLLAMA_API_KEY` défini.

### 3.7 Validation & gates pré-exécution

Pour chaque décision (`daemon.py:1786+`) :

| Vérification | Code rejet |
|---|---|
| Intent valide vs action | `invalid_intent` |
| Exit_plan parsable | `invalid_exit_plan:*` |
| `hard_stop` du bon côté | `invalid_exit_plan:hard_stop_wrong_side` |
| Ouverture sans stop → bloqué | `risk:missing_hard_stop` |
| Confiance ≥ seuil adaptatif | `risk:confidence_below_required` |
| RiskGate.check() | `risk:order_value_exceeded`, `risk:gross_exposure_exceeded`, etc. |

`resolve_exit_plan()` est appliqué aux entrées directes `OPEN_LONG`/`OPEN_SHORT`
(unification avec les armés, D11) : `daemon.py:1860`.

### 3.8 Exécution et persistance

`SimBroker.submit()` → `Fill`. Post-fill :
- `_append_model_performance()` → `state/model_performance.jsonl`
- `create_trade_plan()` → `TradePlanStore` (`state/trade_plans.json`)
- `decision_ledger_store.append()` → `state/decisions.jsonl`
- `_apply_decision_schedule()` → Scheduler (next_wake, indicator_watch créée/annulée)
- `_write_current_report()` → `state/current_report.json`

---

## 4. Les chemins de sortie

Trois chemins distincts. Voir `docs/analysis/comportement-sorties.md` pour le détail
chiffré et les analyses P&L par raison.

### 4.1 Sortie automatique — exit_engine

`exit_engine.evaluate_plan()` (`exit_engine.py:279`) — priorité stricte :
```
hard_stop > max_hold > take_profit > profit_protection > trailing_stop
```

- **hard_stop** : vérifié sur `min(price, bar_low)` (LONG) ou `max(price, bar_high)` (SHORT).
  Fill conservateur : jamais meilleur que le niveau du stop.
- **max_hold** : expiration temporelle (`plan.max_hold_minutes`).
- **take_profit** : paliers fractionnés. `after_fill` peut valoir `close` ou
  `move_stop_to_breakeven`.
- **profit_protection** : réduction partielle (`close_fraction`) quand le drawback
  depuis le high watermark dépasse `trigger_on_giveback_pct`, armé à `arm_at_r` × R.
- **trailing_stop** : types `price` / `percent` / `volatility_multiple`.

Barres 5m agrégées sur `EXIT_CHECK_WINDOW_BARS` (3 × 5 min) pour les plans ouverts
— évite les spikes manqués entre deux barres 15m. Garde temporelle : seules les
barres `ts >= plan.opened_at` comptent. `daemon.py:569`

### 4.2 Sortie discrétionnaire LLM

`intent: CLOSE | REDUCE | REVERSE` → tag `exit_reason = "llm_exit"` (`daemon.py:78`).
Raison : le LLM voit un changement de thèse avant le hard_stop.
`plan_store.close_symbol()` à la clôture.

### 4.3 Sortie via plan armé / resolve (D11)

Les triggers `EXECUTE_ORDER` peuvent inclure des ordres de sortie. Chemin identique
au 4.1 une fois le `TradePlan` créé. La résolution late-binding (D11) s'applique
ici avant tout.

---

## 5. Stops & résolution — `resolve_exit_plan`

`trade_plan.resolve_exit_plan()` (`trade_plan.py:377`) — résout une **intention
paramétrique** en prix absolu. Types supportés :

| Type `hard_stop` | Description | Résolution |
|---|---|---|
| `price` | Prix absolu | Identité |
| `percent` | % de l'entrée | `entry × percent` ± `min_pct/max_pct` |
| `volatility_multiple` | N × vol de référence | `N × reference_volatility` ± clamp |
| `structural` | Ancre chartiste | Extraction barres fraîches : `swing_low/high/vwap` + buffer |

TP supporte également le type `risk_multiple` (R) : `entry ± R × stop_distance`.

La `reference_volatility` est calculée à partir du cockpit daily ou des barres 15m
(`_reference_volatility_for_symbol`, `daemon.py:553`).

La résolution produit un `trace` machine-readable (spec_type, distance, resolved_price,
clamped, reference_volatility…) persisté dans l'événement `armed_plan_resolved`.

**Unification direct/armé (commit `a5c04df`)** : les entrées directes
`OPEN_LONG`/`OPEN_SHORT` passent désormais par le même resolver que les armés.
Seul cas non couvert : un `hard_stop` de type `price` absolu trop serré fourni
directement par le LLM (voir `docs/analysis/comportement-sorties.md` §4).

---

## 6. Univers & rotation (D9 / D10)

### 6.1 Radar (Tier 1 — daily, 0 LLM)

`trader/radar.py` — scan daily EOD du pool (`config/pool.yaml`, ~283 symboles).
Score = `efficacité_tendance × force_relative(benchmark_venue) × amplitude`.
La volatilité est **récompensée** (bornée par ATR floor). Séries ajustées obligatoires.

### 6.2 Rotation & hot-sets par venue (D10)

`trader/rotation_venues.py` — une hot-list par place de marché (TW / EU / US).
Classement intra-venue contre son propre benchmark. Recalculé à la clôture de
chaque session.

Univers actif composé à chaque cycle :
`sticky_all ∪ union(hot-lists des marchés OUVERTS à l'instant t)`
*(La `sleeve_24/5` figurait au design D10 mais n'est pas implémentée — forex/commodity retirés, pool 100 % actions.)*

**Sticky** (`sticky_all`) : positions broker + plans armés + exit-watches + ordres
pending — toujours dans l'univers, hors quota, hors logique d'ouverture.

**Hystérésis** : marge Δ (swap seulement si écart d'attractivité ≥ Δ) + dwell K jours
(l'incumbent est protégé K jours). Sortie d'urgence court-circuite K.

Écriture atomique de `universe.yaml` (idempotente — n'écrit que si le contenu
change, jamais vide → fallback dernier univers valide). `rotation_venues.py`

Override LLM : l'agent peut ajouter/retirer des symboles avec raison loggée
(`rotation_override.py`). Tracé dans `rotation_ledger`.

---

## 7. Veille / réveils — indicator_watch

`trader/indicator_watch.py` — cube `symbol × indicator × timeframe × op × value`.

Trois modes de déclenchement :

| `on_trigger` | Effet | Contexte |
|---|---|---|
| `WAKE` | Réveille le cycle pour ce symbole | Veille simple |
| `WAKE_WITH_ORDER_INTENT` | Réveille + signale une intention d'ordre | Pré-validation |
| `EXECUTE_ORDER` | Exécute l'ordre SANS re-appel LLM | Plan armé D7B |

**INVARIANT ATOMIQUE** : une seule condition rejetée → toute la veille est rejetée
(jamais de watch amputée). `indicator_watch.py:408`

TTL max des plans armés : 240 min (aligné sur la revue périodique garantie). `indicator_watch.py:268`

**exit_watch** : veille attachée à un `TradePlan` ouvert — déclenche `WAKE` quand
une condition technique se réalise post-entrée (ex. `z_score > 1`). Cooldown
configurable (défaut 15 min). `daemon.py:727`

**next_wake** : le LLM peut demander son propre réveil via `next_wake_in_minutes`
dans sa décision — il n'est jamais filtré par le gate de pertinence (autonomie
de planification). `daemon.py:1777`

Opérateurs valides : `>`, `>=`, `<`, `<=`, `==`, `!=`, `abs>`, `abs>=`, `abs<`, `abs<=`.

---

## 8. État persistant — `state/`

| Fichier | Écrit par | Lu par | Contenu |
|---|---|---|---|
| `decisions.jsonl` | `record_decision()` | attribution, CLI, cockpit | Une ligne par décision (action, intent, qty, confidence, rationale, executed, reason…) |
| `model_performance.jsonl` | `_append_model_performance()` | `attribution.py` | Une ligne par fill (entrée + sortie) — base des round-trips |
| `broker.json` | `SimBroker` | daemon (reload à chaque cycle) | Positions paper + historique fills |
| `trade_plans.json` | `TradePlanStore` | exit_engine, daemon | Plans ouverts (hard_stop_price, TPs, trailing, watermarks…) |
| `events.jsonl` | `_append_event()` | monitoring / debug | Événements runtime (cycle_started, armed_plan_resolved, watch_triggered…) |
| `history.jsonl` | `_append_cycle_history()` | CLI status | Résumé par cycle (equity, n_executed) |
| `daemon_status.json` | `_write_status()` | cockpit TUI, CLI | Phase courante, PID, decisions_done |
| `current_report.json` | `_write_current_report()` | cockpit TUI | Rapport complet du cycle en cours |
| `learnings.jsonl` | `record_decision()` | `consolidator` | Notes runtime de l'agent (bornées) |
| `learnings_consolidated.json` | `consolidator` | daemon (contexte LLM) | Patterns consolidés (≤ seuil bruts → consolidation) |

**Scheduler** (`state/scheduler.json`) : next_wake par symbole, indicator_watches,
stale_streaks. Séparé de `broker.json`.

---

## 9. Intégration LLM / acpx

### 9.1 Transport — `llm.py`

`AcpxBackend` (`llm.py:314`) : invoque `acpx --format quiet --allowed-tools "" --no-terminal --non-interactive-permissions deny --model <m> exec [prompt]`.

Prompt labellisé `[casys-trader:runtime-brain]` pour nommage des sessions acpx.
Session jetable (`exec`) — pas d'état partagé entre cycles.

`LlmRouter` cascade sur `OpenAICompatibleBackend` (Ollama cloud) si `acpx` échoue
avec un code retryable (rate-limit, quota). `llm.py:57`

Modèle courant : `gpt-5.5/medium` (Codex Spark, faible latence). `llm.py:22`

### 9.2 Reap des ponts orphelins — `_reap_orphan_bridges`

`llm.py:260` — les processus `codex-acp` (bridge acpx↔codex) démarrés via `setsid`
échappent au `killpg` et survivent à la fin de l'appel. Fix : snapshot `ps` avant
l'appel, diff après, SIGKILL sur les PIDs nouveaux dont le `cwd` correspond au
projet. Déclenché via `_run_one_shot_command.finally`. Cf post-incident
`memory/casys-trader-acpx-bridge-pileup.md`.

### 9.3 `codex_client` — parsing et schéma

`codex_client.py` traduit la réponse texte du LLM en `Decision` (dataclass frozen).
Fail-safe : toute erreur (timeout, JSON invalide, clé manquante) → `Decision.hold(sym, reason)`.
Le LLM ne trade jamais sur une réponse douteuse.

La `Decision` inclut : `action`, `quantity`, `confidence`, `rationale`, `intent`,
`exit_plan`, `indicator_watch`, `cancel_watch_ids`, `next_wake_in_minutes`, `learning`.

---

*Doutes / points à valider humainement listés dans le résumé de livraison.*
