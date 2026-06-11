# Registre des décisions métier — casys-trader

Décisions de **stratégie / produit** (pas d'implémentation). Chaque entrée a un statut :
`💬 en discussion` · `✅ validé` · `🛠 implémenté` · `❄️ gelé/rejeté`.
On ne passe à `✅` qu'après validation explicite d'Erwan. Le code suit, jamais l'inverse.

Format : Contexte → Décision/options → Données à l'appui → Points ouverts.

---

## D1 — Métrique d'audit : « missed » ≠ « bad »  🛠 implémenté (2026-06-11)
**Contexte.** Un HOLD pendant un move était compté « bad » comme un trade raté, gonflant le bad% à ~47 % (lecture trompeuse).
**Décision.** HOLD pendant un move > seuil → verdict `missed` (opportunité laissée passer), distinct de `bad` (vrai trade raté). `missed` compté dans `known` et `nonbad`, jamais dans `bad`.
**Données.** Après fix : bad réel = **1,05 %**, missed = 47,5 %, nonbad = 99 %. Le bot ne se trompe quasi jamais *quand il prend position*.
**Points ouverts.** Aucun. Tests verts (`test_decision_audit.py`, `test_cli_status.py`).

---

## D2 — Biais de régime cross-asset, calculé PAR univers thématique  🛠 implémenté (2026-06-11)
**Contexte.** 95 % des moves ratés arrivent en cluster (plusieurs symboles bougent ensemble), 84 % cohérents directionnellement = régimes risk-on/off. L'agent voit le cockpit de tout l'univers mais personne ne calcule la synthèse directionnelle.
**Décision.** Le code calcule, **par univers thématique**, un indicateur de régime : `{sens, force, up, down}` sur le momentum passé ~45 min, et l'injecte au contexte de l'agent **comme signal d'opportunité actionnable** (pas un frein de plus — cf D6, le consolidé actuel formule déjà le cross-asset comme garde-fou). Seuil de cluster = **2 symboles du même univers**.
**Univers** = `trader/semantic/catalog.py:64` `FAMILIES` (defense, energy, semis_tw, forex_majors, indices, etc.), restreint aux symboles de `config/universe.yaml` actif.
**Données.** Suivre le régime quand ≥70 % de l'univers est aligné = 60 % win, +0,16 %/trade (backtest 6 j, coûts inclus).
**Implémentation (2026-06-11).** `trader/family_regime.py` (compute_family_bias, momentum_from_bars ~45 min, families_for_universe ≥2 membres) ; injecté dans `base_context["regime_families"]` (`trader/daemon.py`) ; guidance « signal d'OPPORTUNITÉ » dans `codex_client.py:_DECISION_GUIDANCE`. TDD (`tests/test_family_regime.py`, `tests/test_daemon_learnings.py`). Rejeu de la nuit du 10/06 : à 17:56 UTC (l'agent disait HOLD), défense ET énergie étaient déjà 100 % down pendant que les indices montaient encore ; à 18:30, bascule risk-off généralisée (4 familles down). La divergence inter-familles précédait le breakdown des indices — exactement le contexte qui manquait à l'agent pour shorter SPY/QQQ/NVDA.
**Points ouverts (implémentation).** Fenêtre de momentum exacte (~45 min testée) ; format compact dans le cockpit ; comment l'agent pondère ce biais vs lecture mono-symbole ; branchement daemon (après validation Erwan du format).

---

## D3 — Réveil sur cluster d'univers (détecteur d'événement)  ✅ validé (principe, 2026-06-11)
**Contexte.** Les moves arrivent toute la séance (85 % en milieu de séance), pas qu'aux ouvertures. Le scheduler par symbole peut rater un événement de régime.
**Décision (proposée, idée d'Erwan).** Quand le code détecte ≥2 symboles d'un même univers qui bougent ensemble, **réveiller l'agent hors planning** en signalant « événement de régime en cours ». L'agent doit savoir que c'est probablement news/macro → intégrer le risque associé (volatilité, retournement). Pont vers D5 (news).
**Données.** Voir D2 (clusters = 95 % des missed).
**Points ouverts.** Seuil/définition du « bouge ensemble » ; fréquence de scan ; éviter les réveils en boucle ; impact du lag data (~10-15 min).

---

## D4 — Ajouter RVOL + ATR expansion au cockpit  ✅ validé (2026-06-11)
**Contexte.** L'agent ne reçoit qu'un score volume grossier (`vs`), pas de RVOL chiffré ni d'ATR expansion — or ce sont les signatures avec edge.
**Décision (proposée).** Injecter par symbole : **RVOL** (volume actuel / moyen) et **ATR expansion** (amplitude barre / ATR récent). Signaux faibles donnés en INPUT, l'agent tranche (juge de confluence).
**Données.** ATR expansion = meilleure signature mono (+0,27 %/trade, 55 % win). Confluence ATR+régime = 75 % win (8 trades). Volume seul = filtre, edge faible (+0,06 %).
**Points ouverts.** Fenêtres de calcul ; sensibilité au lag data ; format compact dans le cockpit.

---

## D5 — Flux news + LLM qualifiant la surprise (les 80 % news-driven)  💬 en discussion
**Contexte.** ~80 % des moves ratés n'ont aucun signal technique préalable → arrivent « sans prévenir » (news/macro). Inatteignables par signal de prix seul.
**Décision (proposée).** Flux news temps réel (Polygon/Benzinga ~30-99 $/mois) + LLM qui interprète *surprise vs consensus* et direction. C'est là que le LLM bat le NLP classique. Le cluster (D3) est le détecteur « pauvre » que la news enrichirait.
**Données.** Littérature : LLM-as-judge intraday prometteur mais **non prouvé** empiriquement. Risques : look-ahead, hallucination → demander des jugements qualitatifs, pas des chiffres.
**Points ouverts.** Coût/abonnement ; latence ; périmètre (macro only vs par-symbole) ; priorité vs D2-D4.

---

## D6 — Boucle de learnings auto-renforçante  🛠 étapes 1-2 implémentées (2026-06-11) ; étapes 3-4 à discuter
**Contexte.** L'agent reçoit à chaque décision : consolidés `global` + `by_symbol` ENTIERS **+ les 3 derniers bruts** (`consolidator.py:199`, tronqué à 3). Vérif du contenu réel :
- `global` = **9 notes "robustesse élevée", TOUTES des règles d'abstention** ("HOLD en range tant que |z|<2 et HTF non aligné", "z extrême seul insuffisant", "filtrer les signaux isolés"…).
- `by_symbol` = **18 symboles**, chacun la même règle déclinée.
- La note globale n°8 dit *« intégrer la cohérence cross-asset avant d'ouvrir »* — donc le cross-asset (D2) est déjà connu de l'agent, **mais formulé comme un frein, pas un signal** → D2 doit l'injecter comme opportunité actionnable.
- La déduplication FONCTIONNE (consolidé propre) ; le problème est le **contenu** : 27 façons dédupliquées de dire « ne trade pas ».
Mécanique : agent écrit un HOLD → consolidé en interdiction → relu → re-HOLD. Aucun contre-signal. Le gate confidence (0,7→0,9) n'est pas le bottleneck ; c'est la confidence *produite* qui bloque.
**Proposition Opus (2026-06-11, à valider par Erwan).** Diagnostic mécanique : (1) incitation asymétrique — l'agent n'écrit des learnings que sur HOLD, jamais de contre-signal ; (2) le prompt de consolidation dit « préserve les entrées stables » → la règle majoritaire (HOLD) gagne toujours ; (3) réinjection maximale sans distinction garde-fou humain / habitude machine ; (4) aucun feedback contrafactuel (les `missed` ne génèrent jamais de learning « j'aurais dû entrer »). Séquence proposée :
- **Étape 1 (quick wins, réversibles)** : reformuler le prompt de consolidation (cap dur ≤5 règles d'abstention, priorité aux patterns d'entrée, `consolidator.py:213`) ; ne plus réinjecter les bruts quand un consolidé existe (`consolidator.py:196`) ; purger le consolidé actuel (backup) et laisser reconstruire avec le nouveau prompt.
- **Étape 2** : séparer `guardrails` (humain, immuable, `mandate/guardrails.json`) des `patterns` (machine, évoluables) — migrer les 3-4 vraies règles invariantes AVANT la purge.
- **Étape 3** : boucle contrafactuelle — module `missed_moves` qui injecte les HOLD récents où le marché a bougé (fenêtre fermée H-2 pour éviter le hindsight), pour que l'agent calibre et écrive des contre-learnings.
- **Étape 4** : `by_symbol` limité au symbole courant (−50 % tokens).
⚠️ Condition préalable confirmée par Opus : sans étapes 1-2, les nouveaux signaux D2/D4 entreront en conflit avec le mur d'abstention → HOLD par précaution.
**Données.** Débrider seul = 50/50 sur le sens. L'edge vient du *sens* (D2/D4), pas du volume de trades.
**Implémenté (étapes 1-2, validées par Erwan, 2026-06-11).** Nouveau prompt de consolidation (cap ≤5 abstentions, priorité aux entrées, robustesse élevée = ≥3 occurrences — `consolidator.py:build_consolidation_prompt`) ; bruts non réinjectés quand un consolidé existe (`build_context_learnings`) ; `mandate/guardrails.json` (invariant : pas d'ouverture sans stop) injecté à part + guidance « guardrails = invariants, le reste = patterns remettables en question ». Consolidé purgé le 2026-06-11 (backup `state/learnings_consolidated.json.bak-20260611`) ; les 200 bruts seront reconsolidés au prochain seuil avec le nouveau prompt.
**Points ouverts.** Étape 3 (missed_moves contrafactuels, fenêtre H-2) et étape 4 (by_symbol scopé) à discuter ; surveiller le premier consolidé regénéré (équilibre entrées/abstentions) ; mesure A/B du bad% réel post-changement.

---

## D7 — LLM planificateur actif plutôt qu'opérateur de polling  🛠 étages A et B implémentés (2026-06-11)
**Contexte.** 92 appels LLM décideur / 24 h (~4/h, prompt ~15-20k tokens), pour ~99 % de HOLD. Le LLM est réveillé par le planning pour constater qu'il ne se passe rien. Coût élevé, valeur marginale quasi nulle hors événements. Proposition Erwan : passer le LLM d'opérateur (appelé à chaque cycle dû) à planificateur actif (il arme des plans, le code exécute).
**Infra déjà en place.** `indicator_watch` (+ `WAKE_WITH_ORDER_INTENT`), `exit_plan` exécuté mécaniquement, `exit_watch`, `next_wake_in_minutes`, `trade_plan`, batch 1-appel/cycle, et désormais `regime_families` (événements de régime calculés par le code).
**Options.**
- **A — Gate de pertinence en code (quick win, sans changement d'archi).** N'inclure dans le batch que les symboles avec un signal réel (|z| élevé, ATR expansion, RVOL, régime fort, trigger de watch, position ouverte, événement de plan). Nuit forex calme → 0 appel. Estimation : −50 à −70 % d'appels, qualité intacte (on n'appelait le LLM que pour dire HOLD).
- **B — Plans armés pré-autorisés.** Quand le LLM est appelé, il émet des PLANS : entrée conditionnelle (watch + sens + taille + exit_plan + invalidation + TTL). Au déclenchement, le daemon EXÉCUTE SANS re-appeler le LLM — le gate de risque déterministe (hard_stop obligatoire, plafonds) reste le fusible. Appels LLM restants : revues stratégiques par session/univers + événements de régime + invalidations. Estimation : ~10-20 appels/jour.
- **C — Séquence recommandée : A puis B.** A est pur code, mesurable, réversible, zéro risque qualité. B ensuite, une fois A mesuré.
**Trade-offs de B (à trancher).**
1. Plan périmé : marché qui change après l'armement → mitigé par TTL courts, invalidation watch, annulation sur bascule de régime.
2. Pré-autorisation : aujourd'hui un trigger WAKE_WITH_ORDER_INTENT « repasse par Codex » — exécuter direct transfère le jugement au moment de l'armement. Le risque résiduel est borné par le gate déterministe.
3. Moins d'adaptabilité → compensée par les réveils événementiels (cluster D3, triggers).
**Arbitrage tranché en plus (Erwan, 2026-06-11) : l'agent GARDE son autonomie d'auto-réveil** — un `next_wake_in_minutes` qu'il a demandé n'est jamais filtré ; cible souple ~−50 % d'appels, pas une coupure totale.
**Étage A implémenté (2026-06-11).** `trader/relevance_gate.py` (fonctions pures) + câblage daemon : le polling par défaut sur symbole calme ne consomme plus d'appel (décision tracée `quiet_gate`) ; passent toujours — réveil demandé par l'agent (`Scheduler.has_symbol_wake`), triggers, position ouverte, régime de famille fort (≥0,70), signaux cockpit (sig/stretched), revue périodique garantie (4 h, et premier réveil post-restart). Guidance ajoutée : préférer les `indicator_watch` aux réveils courts. TDD (`tests/test_relevance_gate.py`, 8 tests). À mesurer : appels/24 h avant→après.
**Motivation supplémentaire (Erwan, 2026-06-11) — le mode planificateur force l'usage du modèle sémantique.** En opérateur, l'agent peut répondre HOLD en prose sans jamais mobiliser la couche sémantique. En planificateur, un scénario DOIT s'exprimer dans le vocabulaire gouverné (`indicator_watch` : cube symbol × indicator × timeframe × op × value ; `exit_plan` structuré) → sorties machine-validables (fast-fail sur indicateur halluciné), plans auditables et rejouables a posteriori, et learnings rattachables à l'issue d'un plan précis plutôt qu'à un sentiment. Le planificateur transforme le jugement LLM en artefacts vérifiables — exactement l'esprit AX.
**Arbitrage 1 tranché (Erwan, 2026-06-11) : exécution DIRECTE des plans armés, sans re-appel LLM.** Le LLM fait des ordres selon des scénarios mais n'exécute pas en direct ; il garde la main pour se réveiller et poser/ajuster ses plans. Le gate de risque déterministe reste le fusible.
**Étage B implémenté (MVP, 2026-06-11, consultation Codex intégrée).** `on_trigger:"EXECUTE_ORDER"` sur les indicator_watch : le LLM arme un scénario complet (`order` = intent OPEN_*, qty, confidence, exit_plan avec hard_stop OBLIGATOIRE, rationale) ; au déclenchement le daemon exécute SANS re-appel via la boucle d'exécution normale (tous les gates de risque s'appliquent : missing_hard_stop, clamp 1 %, order_value, gross). Sécurités au déclenchement : si le prix a déjà franchi le stop, si une position existe déjà ou si les données sont stale → annulation + RÉVEIL du planificateur avec le contexte (événement `armed_plan_cancelled`). Contrat strict à l'armement (fast-fail) sinon dégradation en WAKE_WITH_ORDER_INTENT ; TTL max 4 h, aligné sur la revue périodique (corrigé par Erwan : un TTL court forçait des réveils de ré-armement, l'expiration étant silencieuse) ; provenance ledger `source="armed_plan"` + `armed_plan_id` ; `_LAST_LLM_AT` mis à jour seulement si le modèle a réellement statué. TDD : `tests/test_armed_plans.py` (5), `tests/test_indicator_watch.py` (+5), contrat agent exposé (vocabulaire + guidance).
**Décision Erwan (2026-06-11) : PLUSIEURS scénarios par symbole.** Les plans armés sont des scénarios alternatifs (cassure haute → long / cassure basse → short) : ils coexistent sur un même symbole, un seul se réalisera (les autres s'annulent via position_exists ou expirent). Implémenté : le scheduler ne remplace plus les plans armés entre eux (les veilles simples gardent le remplacement) ; si plusieurs scénarios du même symbole déclenchent au même cycle → conflit, pas d'exécution arbitraire, réveil du planificateur (`armed_plan_conflict`). Rôle « planificateur » cadré explicitement dans le prompt (headers + échelle d'engagement en guidance) et dans le mandat.
**Backlog post-MVP (findings Codex non bloquants).** Debounce du gate par raison/symbole (un régime fort persistant re-déclenche toutes les 30 min — principal reliquat de coût) ; persister `last_llm_at` (volatile → batch global à chaque restart) ; plafond GLOBAL de plans armés actifs ; dédup d'un scénario identique ré-armé ; `max_price_drift` numérique ; télémétrie complète (created/expired/blocked + âges).
**Points ouverts.** (2) Budget d'appels cible/jour (~20 en cible d'observabilité). (3) Cadence des revues stratégiques (proposition : une par ouverture de session TW/EU/US). Mesurer A+B sur 24-48 h après restart.
