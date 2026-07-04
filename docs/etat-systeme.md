# État du système — casys-trader

> Document vivant : mis à jour à chaque évolution majeure. Détail des
> mécanismes dans `docs/architecture.md` ; décisions métier dans
> `docs/decisions/registre-decisions-metier.md`.

**Dernière mise à jour : 2026-07-04**

## Ce qui tourne

- **Daemon** `trader.daemon --live` (paper), supervisé (`cockpit_supervisor`,
  anti-doublon PID). Startup : rotation des ledgers + log `[config] …
  agent_tools=True`. Code committé propre (traçabilité `code_version` saine).
- **Orchestration = file durable SQLite (task-ledger — ACTIVÉE en paper 04/07)** :
  état en `state/casys.db` (`CASYS_STATE_BACKEND=sqlite`), `decide` et `execute`
  routés par la file (`CASYS_QUEUE_DECIDE_ENABLED` / `CASYS_QUEUE_EXECUTE_ENABLED`,
  outbox transactionnel), sonde `[state-compare]` par cycle = `identical=True`. Les
  chemins synchrones restent en fallback (flags off) jusqu'au gommage. Réf :
  `docs/reference/task-queue.md`. ⚠️ Vrai trafic decide/execute à observer à la
  réouverture des marchés (week-end `due=0`).
- **Transport LLM** : fork `Casys-AI/acpx#casys-patches` via `TRADER_ACPX_BIN`
  (erreurs quiet structurées, bridges jamais orphelins) ; modèle gpt-5.5 →
  fallback sonnet → ollama-cloud ; `parallelism=5` (backpressure AIMD ; monté depuis
  le bridage historique à 1, test observé lundi).
  ⚠️ Vérification en attente : preuve par process que le fork sert les appels
  (poll `ps` sur `…/acpx/dist/cli.js` pendant un batch actif).
- **Cockpit TUI** + gonzo sur les logs.

## Capacités de l'agent (brain runtime)

- **Cockpit compact poussé** (faits code-calculés) + **9 outils en pull**
  (flag actif) : fraîcheur, plans armés, risque de position, attribution,
  décisions récentes, cube d'indicateurs, découverte sémantique ×2, et
  **`recall_learnings`** — sa mémoire pondérée par les résultats réels
  (2 091 notes scorées, win rate historique 34 %, dispersion 3-100 % par
  symbole). Une tournée max, tout tracé.
- **Outils d'action LIVRÉS + LIVE en paper** (Phase 6) : `propose_order`
  (OPEN/CLOSE/REDUCE/REVERSE/ADD, sizing par `risk_pct`), `amend_exit`,
  `set_next_wake` — le daemon reste seul exécuteur (exécution/exit-plan/RiskGate
  inchangés). Réf : `docs/reference/agent-tools.md`.
- **Horodatage** : `now_human` (jour + heure UTC) + `market_clocks` (heure locale
  de chaque place, tri ouest→est) — lève l'ambiguïté jour/session, y compris piloté
  depuis Taiwan (cf. `docs/reference/agent-context.md`).
- Décision finale = JSON gaté (exécution/exit-plan/RiskGate inchangés).

## Données et mémoire

- **Ledger** : vif = mois courant (~16 Mo), mois passés en
  `state/archive/*.jsonl.gz` (rotation crash-safe, validée Codex, artefacts
  prod vérifiés sains — 19 105 decision_id uniques, zéro doublon).
- **Learnings** : plus rien ne se jette (évincés + historique des consolidés
  archivés ; backfill historique 1 867 notes). Store de recall
  `state/learnings.db` (dérivé, reconstructible).
- **Collecte macro/news (P1a — en livraison)** : items de news persistés,
  `macro_next` (FOMC/CPI) par décision, séries DBnomics quotidiennes.
  L'analyste-news LLM viendra quand le stock aura 2-3 semaines.

## Mesures en cours (les données décident)

| Mesure | Où | Échéance |
|---|---|---|
| Usage des outils / du recall par l'agent | `runtime.tool_calls`, table `recalls` | continu, premier bilan ~J+2 |
| Veto earnings (collecte saine, inter-saison confirmée) | `scripts/news_attribution_measure.py` | **mi-août** (saison Q2, viser n≥30/bucket) |
| Efficacité du recall vs push linéaire | `decision_bench` A/B (phase ④) | quand `recalls` a du volume |
| CPU `deciding_batch` post-cache | observation | immédiat |

## Chantiers à venir (priorités)

1. **File durable — observation + gommage** : observer le vrai trafic
   decide/execute-via-file à la réouverture (redémarrage lundi charge `market_clocks`
   + `parallelism=5`), puis gommer l'ancien mode (fallbacks synchrones + double-write
   JSON + sonde shadow ; 5 étapes gated, lecteurs JSON d'abord).
2. **Analyste-news** (P2 macro) — attend le stock d'items.
3. **MemRL + decay calibré** (phase ③ recall) — attend le volume de `recalls`.
4. **Patch rétention sessions acpx** (fork) — règle les 1,3 Go de ~/.acpx.
5. PRs upstream acpx (2 branches `fix/*` prêtes) ; EODHD ~60 €/mois si le
   calendrier earnings EU/TW prospectif manque à l'analyste.

## Process de dev (rappels)

- Sous-agents Claude pour le travail (défaut Fable pour le code), **Codex en
  cross-check final pour TOUT lot touchant la prod** (leçon du 02/07 — la
  passe de rattrapage a trouvé un CRITICAL sur la rotation), sessions acpx
  nommées et fermées après usage.
- `make check` (ruff + 1 800+ tests) ; jamais de « vert » sans exit code lu.
- Restart daemon : fenêtre inter-cycle (`phase=cycle_completed`), SIGINT.
