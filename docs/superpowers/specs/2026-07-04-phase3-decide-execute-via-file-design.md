# Phase 3 — `decide` + `execute` via la file durable (Lot A + Lot B, flags orthogonaux)

> Sous-phase du chantier task-ledger. Design d'architecture général :
> `docs/superpowers/specs/2026-07-03-task-ledger-durable-queue-design.md` (§4.1, §4.3bis,
> §4.4, §4.5, §4.6). Ce document précise le **comment** de la bascule `decide`/`execute → file`.
> Validé avec Erwan le 2026-07-04 (Lot A incrémental + Lot B derrière flag orthogonal).

## 1. Objectif

Basculer l'orchestration du daemon vers la **file durable** (`trader/queue/`), en **deux
étages indépendants activables séparément** :

- **Lot A — `decide` via file** (flag `CASYS_QUEUE_DECIDE_ENABLED`) : les décisions LLM
  passent par la file au lieu de `_batch_decide` synchrone.
- **Lot B — `execute` via file** (flag `CASYS_QUEUE_EXECUTE_ENABLED`) : les ordres issus des
  décisions passent par la file au lieu d'être exécutés synchrones dans `run_cycle`.

Bénéfices : fin du HOLD-par-saturation (retry/requeue), backpressure adaptative acpx (AIMD),
isolation par symbole, durabilité/reprise (crash-safe), et — quand les deux sont on — voie
rapide sorties (exécution découplée du batch LLM).

**Hors périmètre** : Lot C (live IB, `IBBroker` inexistant, gated §7 du design chantier).

## 2. Décisions validées (2026-07-04)

- **Deux flags ORTHOGONAUX**, off par défaut (prod strictement inchangée) :
  - `decide`-flag change **comment on obtient les Decisions** (file grain-symbole vs batch).
  - `execute`-flag change **comment une Decision devient un ordre** (execute_order via file vs
    submit synchrone).
  - 4 combinaisons valides et testables (off/off, on/off, off/on, on/on).
- **Grain-symbole (K=1)** pour `decide` : `1 tâche = 1 symbole = 1 appel acpx`
  (`partition_key=symbole`). Conforme §4.3bis + demande Erwan.
- **Budget de temps du cycle** = `CASYS_DECISION_TIMEOUT_S` (900s) : plafond d'attente des
  tâches `decide` d'un cycle ; au-delà, non-finies `pending` (redécidées au cycle suivant),
  symbole **skippé** (pas de HOLD synthétique).
- **Lot B sûr en paper** : `SimBroker.submit` synchrone/déterministe → `execute_order` =
  `revalidation + submit + plan + done` en **1 transaction**, idempotent, **aucune
  réconciliation, pas de STOP** (Codex GO ; le STOP visait le live IB).
- **Réversibilité (strangler)** : chaque flag est un rollback indépendant.

## 3. Architecture

```
run_cycle (PRODUCTEUR, synchrone)
  1. calcule `decidable` (gate de pertinence — INCHANGÉ)

  2. DECIDE :
     - decide-flag OFF → `_batch_decide` actuel (inchangé)
     - decide-flag ON  → enfile 1 tâche `decide` par symbole
         (kind=decide, partition_key=sym, resource=acpx, dedup_key=cycle:sym)
         pool workers → decide_one(sym) → acpx → Decision dans task.result
         run_cycle ATTEND ses tâches (budget) + COLLECTE ; non-finies = skip

  3. fusion armed_decisions + order_admission (arbitrage marge/conviction — INCHANGÉ,
     sur l'ensemble des Decisions du cycle)

  4. EXECUTE (les ordres retenus par order_admission) :
     - execute-flag OFF → `SimBroker.submit` synchrone dans run_cycle (inchangé)
     - execute-flag ON  → enfile 1 tâche `execute_order` par ordre retenu
         (kind=execute_order, resource=portfolio, partition=portfolio, priorité 1,
          payload={ordre revalidé}, dedup_key idempotent)
         worker execute_order → revalidation (cash/prix) + submit + plan + mark done
           dans UNE transaction SQLite

Pool de N worker-threads (démarré au boot, vit tant que le daemon vit)
  claim resource-aware : acpx=M (backpressure AIMD §4.5), portfolio=1 (verrou mutation)
  UNE seule ressource par tâche (pas d'acquisition imbriquée → zéro deadlock)
  heartbeat/lease + recover_on_boot ; fencing token sur complete/fail
```

**Backpressure `M`** (decide) : AIMD double-signal (réutilise `llm.py:144-168`) — signal dur
(overload/internal/vide → `M ×0.5`), signal doux (latence p50/p95 → gel increase), increase
prudent (`M +1`, plafond). **`portfolio=1`** (execute) : sérialise cash/positions, cohérence
garantie même sous plusieurs `execute_order`.

## 4. Composants

**Nouveaux** :
- `trader/queue/decide_pool.py` : pool de workers + cycle de vie (boot/heartbeat/arrêt).
  Générique (sert decide ET execute_order — mêmes primitives Worker/ResourcePools).
- `trader/application/decide_one.py` : handler `decide` — prompt 1-symbole (extrait de
  `decide_batch`), acpx, `Decision`. Réutilise le contexte `per_symbol`.
- `trader/application/execute_order_handler.py` : handler `execute_order` — revalidation
  (`order_admission` finale + checks cash/prix `daemon.py:2477/2606`) + `SimBroker.submit` +
  plan + `done` en 1 tx.
- `trader/application/queue_dispatch.py` : producteur + collecte dans `run_cycle` (enfile,
  attend budget, collecte, applique skip ; enfile execute_order si flag on).

**Réutilisés (inchangés)** : `task_ledger`/`pools`/`worker` (Phase 0), classif erreurs +
lifecycle acpx `llm.py`, `order_admission`, `SimBroker`, gate, `armed_decisions`, recording,
scheduler, consolidation.

## 5. Ce qui NE change PAS (surface de risque bornée)

Gate de pertinence, `armed_decisions`, `order_admission` (arbitrage marge — reste dans
run_cycle sur l'ensemble), recording (`decisions.jsonl`), scheduler, consolidation, backend
d'état (reste `json` par défaut). Les chemins `_batch_decide` et exécution synchrone restent
présents comme fallback (flags off).

## 6. Invariants (tests TDD)

**Lot A (decide) :**
1. decide-flag off → `_batch_decide` **strictement inchangé** (non-régression).
2. decide-flag on → chaque symbole décidable = **exactement une** tâche `decide`
   (`uniq_active_kind_partition`).
3. `decide` échec retryable → **requeue** (backoff), pas HOLD.
4. Budget écoulé → symboles non finis **skippés** (pas de décision fabriquée), tâches `pending`.
5. Crash pendant `decide` → `recover_on_boot` remet `running`→`pending` (pas perdu ni doublé).
6. Backpressure : erreurs overload → `M` décroît ; série saine → `M` remonte (bornes).

**Lot B (execute) :**
7. execute-flag off → `SimBroker.submit` synchrone **inchangé** (non-régression).
8. execute-flag on → un ordre retenu = **exactement une** tâche `execute_order` ; rejeu
   (même dedup_key) n'exécute pas deux fois (idempotence).
9. `execute_order` = submit + plan + `done` **atomiques** (échec après submit → rollback,
   pas de plan orphelin ni de cash incohérent).
10. `portfolio=1` : deux `execute_order` concurrents sérialisés (jamais deux mutations
    portefeuille en parallèle).
11. Parité : état broker/plans après exécution via file == exécution synchrone, mêmes entrées.

## 7. Risques & points ouverts

- **`decide_batch` → `decide_one`** : fidélité du prompt 1-symbole (le §4.3bis argue que la
  vue comparative est redondante) — vigilance en review.
- **`order_admission` reste dans run_cycle** (arbitrage sur l'ensemble) ; le worker
  `execute_order` refait la revalidation *finale* (cash/prix au moment du submit). Bien séparer
  arbitrage (cycle) vs revalidation (worker).
- **Coût acpx** (grain-symbole, +appels) : atténué réveils intelligents + prompt caching ; à
  mesurer flag-on.
- **Cohabitation shadow (Phase 2)** : ledgers distincts (`task_ledger.db` vs `shadow_queue.db`).
- **Ordre d'activation recommandé** : decide-flag d'abord (observer), puis execute-flag — mais
  techniquement indépendants.
