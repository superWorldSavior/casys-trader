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
**Univers** = `trader/domain/semantic/catalog.py` `FAMILIES` (defense, energy, semis_tw, forex_majors, indices, etc.), restreint aux symboles de `config/universe.yaml` actif.
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
**Arbitrage tranché en plus (Erwan, 2026-06-11 ; précision de cadence 2026-08-31) : l'agent GARDE son autonomie d'auto-réveil** — un `next_wake_in_minutes` demandé reste prioritaire lorsqu'il est plus proche ; s'il est plus tard, la borne système avance la revue à 1 h hot / 4 h calme. La cible reste souple (~−50 % d'appels), pas une coupure totale.
**Étage A implémenté (2026-06-11, cadence position amendée 2026-08-31).** `trader/relevance_gate.py` (fonctions pures) + câblage daemon : le polling par défaut sur symbole calme ne consomme plus d'appel (décision tracée `quiet_gate`). Passent toujours : réveil demandé par l'agent (`Scheduler.has_symbol_wake`), trigger explicite, première revue sans état durable valide et revue périodique garantie (4 h). Une position ouverte ou un setup avec watch active est revu au plus tard toutes les **1 h pendant la séance** ; cette cadence est contournée par les réveils agent (dont post-entry), triggers (`exit_watch` inclus), et transitions matérielles de régime fort / signal HTF : apparition, inversion, disparition, puis réapparition après revue de l'absence. Les triggers restent mécaniques sur barres 15 min clôturées ; les thèses restent 1d/4h. Hard exits, RiskGate et exécution directe des plans armés D7B restent en dehors de ce gate de pertinence. Guidance ajoutée : préférer les `indicator_watch` aux réveils courts. TDD dans `tests/test_relevance_gate.py`. À mesurer : appels/24 h avant→après.
**Motivation supplémentaire (Erwan, 2026-06-11) — le mode planificateur force l'usage du modèle sémantique.** En opérateur, l'agent peut répondre HOLD en prose sans jamais mobiliser la couche sémantique. En planificateur, un scénario DOIT s'exprimer dans le vocabulaire gouverné (`indicator_watch` : cube symbol × indicator × timeframe × op × value ; `exit_plan` structuré) → sorties machine-validables (fast-fail sur indicateur halluciné), plans auditables et rejouables a posteriori, et learnings rattachables à l'issue d'un plan précis plutôt qu'à un sentiment. Le planificateur transforme le jugement LLM en artefacts vérifiables — exactement l'esprit AX.
**Arbitrage 1 tranché (Erwan, 2026-06-11) : exécution DIRECTE des plans armés, sans re-appel LLM.** Le LLM fait des ordres selon des scénarios mais n'exécute pas en direct ; il garde la main pour se réveiller et poser/ajuster ses plans. Le gate de risque déterministe reste le fusible.
**Étage B implémenté (MVP, 2026-06-11, consultation Codex intégrée).** `on_trigger:"EXECUTE_ORDER"` sur les indicator_watch : le LLM arme un scénario complet (`order` = intent OPEN_*, qty, confidence, exit_plan avec hard_stop OBLIGATOIRE, rationale) ; au déclenchement le daemon exécute SANS re-appel via la boucle d'exécution normale (tous les gates de risque s'appliquent : missing_hard_stop, avertissement de sizing 1 % quand le stop existe, order_value, position_value, gross). Sécurités au déclenchement : si le prix a déjà franchi le stop, si une position existe déjà ou si les données sont stale → annulation + RÉVEIL du planificateur avec le contexte (événement `armed_plan_cancelled`). Contrat strict à l'armement (fast-fail) sinon dégradation en WAKE_WITH_ORDER_INTENT ; TTL max 4 h, aligné sur la revue périodique (corrigé par Erwan : un TTL court forçait des réveils de ré-armement, l'expiration étant silencieuse) ; provenance ledger `source="armed_plan"` + `armed_plan_id` ; `_LAST_LLM_AT` mis à jour seulement si le modèle a réellement statué. TDD : `tests/test_armed_plans.py` (5), `tests/test_indicator_watch.py` (+5), contrat agent exposé (vocabulaire + guidance).
**Décision Erwan (2026-06-11) : PLUSIEURS scénarios par symbole.** Les plans armés sont des scénarios alternatifs (cassure haute → long / cassure basse → short) : ils coexistent sur un même symbole, un seul se réalisera (les autres s'annulent via position_exists ou expirent). Implémenté : le scheduler ne remplace plus les plans armés entre eux (les veilles simples gardent le remplacement) ; si plusieurs scénarios du même symbole déclenchent au même cycle → conflit, pas d'exécution arbitraire, réveil du planificateur (`armed_plan_conflict`). Rôle « planificateur » cadré explicitement dans le prompt (headers + échelle d'engagement en guidance) et dans le mandat.
**Backlog post-MVP (findings Codex non bloquants).** Debounce du gate par raison/symbole (un régime fort persistant re-déclenche toutes les 30 min — principal reliquat de coût) ; persister `last_llm_at` (volatile → batch global à chaque restart) (MAJ 2026-08-16 : fermé par beb9e26 — voir D17) ; plafond GLOBAL de plans armés actifs ; dédup d'un scénario identique ré-armé ; `max_price_drift` numérique ; télémétrie complète (created/expired/blocked + âges).
**Points ouverts.** (2) Budget d'appels cible/jour (~20 en cible d'observabilité). (3) Cadence des revues stratégiques (proposition : une par ouverture de session TW/EU/US). Mesurer A+B sur 24-48 h après restart.

---

## D8 — Backtest à deux étages : replayer mécanique de plans + évaluation forward  ✅ validé (2026-06-11)
**Contexte.** Le moteur de backtest (`backtest/engine.py:run_backtest`) rejoue des décisions instantanées par timestamp : il ignore les exit_plans mécaniques (écart pré-existant), les watches, les plans armés et le gate — il ne représente plus le comportement live du système planificateur. À l'inverse, un plan armé est un artefact 100 % mécanique (condition + sens + taille + stop + TTL) : rejouable déterministiquement, zéro appel LLM.
**Décisions (Erwan, 2026-06-11).**
1. **Étage 1 — replayer mécanique de plans : ÉVALUATION A POSTERIORI uniquement.** Outil de mesure (taux de déclenchement, annulations, P&L, stops touchés) pour juger la qualité des scénarios de l'agent et nourrir les learnings. PAS de validation a priori : l'agent reste libre d'armer, le gate de risque reste le seul fusible. (La validation a priori reste une option future si la mesure montre des scénarios systématiquement mauvais.)
2. **Construction MAINTENANT**, en TDD sur plans synthétiques — prêt quand les premiers plans réels arriveront (provenance `armed_plan` au ledger).
3. **Ancien moteur conservé comme baseline** (stratégies codées de référence), documenté comme NE représentant PAS le comportement live de l'agent.
**Étage 2 (forward, attend la donnée).** Qualité des scénarios réellement armés : taux déclenchés/annulés/expirés, P&L par plan, par famille et par condition — quelques jours de plans réels nécessaires.
**Points ouverts.** Granularité du replay (barres 15 m) ; modélisation des take_profits fractionnés ; intégration au CLI (`casys-trader plans replay`).

---

## D9 — Veille à deux niveaux : radar large + rotation du hot-set  ✅ validé (design, 2026-06-15)
**Contexte.** L'univers est une liste unique curée à la main (`config/universe.yaml`, ~13 symboles) et **tout** passe au hot-path 15m (cher en appels LLM). On ne peut pas élargir la veille sans exploser le coût. On veut que **le marché choisisse ce qu'on trade** : un univers qui surperforme reçoit la veille fine, un qui décroche en sort — sans payer la veille fine sur tout, tout le temps.
**Décision (Erwan, 2026-06-15).** Séparer la veille en **deux niveaux** :
- **Tier 1 — radar** : grand pool liquide borné (~500-800, `config/pool.yaml`), scanné en **daily EOD, 100 % code, 0 LLM**. Sortie = un **classement** (éligibilité + biais directionnel), pas des trades.
- **Tier 2 — hot-path 15m existant**, inchangé, mais opérant sur un **set dynamique** (`universe.yaml` devient un **output généré**, cap initial **M = 25** symboles, porté à **50** en paper le 2026-07-06, sticky hors quota).

Décisions verrouillées :
1. **Décideur = code défaut déterministe + agent override tracé.** Le radar produit le hot-set par défaut (reproductible/backtestable) ; l'agent reçoit le radar complet (1 appel/jour) et ajoute/retire avec **raison loggée**. Pas « l'agent décide tout » : un LLM non-déterministe dans la boucle de sélection casserait la backtestabilité de la rotation, qui *est* la nouvelle stratégie.
2. **Score** = `efficacité_tendance × force_relative(benchmark de la venue) × amplitude` — la **volatilité est RÉCOMPENSÉE (bornée) et requise (plancher ATR%), pas pénalisée**. Règle métier clé d'Erwan : on veut « de la stabilité dans la tendance ET de la volatilité en même temps » (sinon rien à trader en 15m). Séries ajustées (splits/dividendes) obligatoires.
3. **Contrat d'horizon** : le radar donne l'éligibilité + le biais ; le **timing d'entrée reste au hot-path 15m**. Le radar dit « surveille de près », pas « achète maintenant ».
4. **Concentration assumée** : pas de garde-fou de concentration à la sélection (rotation quotidienne + conviction + même thèse sur plusieurs venues = plus de trades). Réserve actée : `max_gross_exposure` borne le levier nominal mais **pas la corrélation** → drawdown corrélé intra-jour assumé ; diversification non-objectif.
5. **Cadence** : radar daily + **hystérésis** (marge Δ, dwell K jours) **+ sortie d'urgence** (seuil absolu/gap, court-circuite K). 
6. **Tilt de conviction optionnel** (`conviction.yaml`, vide par défaut, borné) ; **exclusions dures** (futures data-différée) dans `pool.yaml` — PAS `regime.yaml` (qui ne sert que l'attribution et ne liste pas `NG=F`).
7. **Sticky hors quota, calculé AVANT l'écriture** : union positions + plans armés + exit watches + ordres pending — sinon `reconcile_universe` purge les gardes d'une position hors hot-set.
8. **Mesure d'alpha en parallèle de l'impl** : `rotation_ledger` (défaut vs final) + bench de rotation as-of (dynamique vs statique vs top-liquidité), P&L 15m net conditionné par appartenance au hot-set.

S'appuie sur D2 (`family_regime`, biais par univers — le radar en est le frère long-horizon) et D7 (`relevance_gate`, gate de coût du hot-path).
**Données.** Design relu par review Codex indépendante (fan-out 3 axes : stratégie / edge cases / faisabilité), 28 findings → 14 retenus. 2 bloquants corrigés (écriture atomique de `universe.yaml` ; sticky avant écriture). 3 erreurs factuelles du spec corrigées (`starting_cash` → `portfolio.yaml` repointant `daemon.py:1102` + `stats.py:114` ; `NG=F` absent de `regime.yaml` ; contrefactuel non branchable sur `decision-bench` → `rotation_ledger` dédié).
**Design détaillé.** livré (historique dans `git log` ; spec supprimée post-livraison).
**Points ouverts.** Calibration (forme additive/multiplicative du score, gestion signe long/short, fenêtres, `atr_floor`, plafond amplitude, Δ, K, seuils de sortie d'urgence, benchmarks par venue) ; sizing corrélation-aware (réserve, hors scope du design) ; implémentation (plan à dérouler via `writing-plans`).
**Implémenté (2026-06-15).** Mécanisme complet sur `main` : radar (`trader/radar.py`, `radar_data.py`), rotation (`rotation.py`, `rotation_state.py`, `rotation_ledger.py`, `rotation_bench.py`, `rotation_wiring.py`, `rotation_collectors.py`), gate daemon (`rotation_daemon.py` câblé dans la boucle), configs (`pool.yaml` étendu ~43 symboles dont 30 valeurs TW sectorisées, `radar.yaml`, `conviction.yaml`, `sessions.yaml`, `portfolio.yaml`). Override LLM branché (`override_enabled`, fail-safe). Run réel validé (fetch yahoo 42 symboles → hot-set écrit). 2ᵉ review Codex (cœur) : 7 findings corrigés. ~1128 tests verts.

> **Addendum 2026-07-10 :** D15 supersède uniquement le cap paper 50 et la
> formulation de propriété finale « défaut + override ». Le radar déterministe,
> l'hystérésis, la baseline backtestable et le fail-safe de D9 restent valides.

> **Addendum calibration shadow 2026-07-10.** Audit du cache courant sur 412
> symboles éligibles : contribution trend médiane `0,2072`, force relative
> `0,0420`, soit 82,2 % de part trend médiane ; bonus amplitude médian 1,66 %.
> Sur 186 signaux short, 168 sous-performent leur benchmark et la formule legacy
> fait alors de la force relative un offset positif du score short. Décision :
> ne pas modifier la production à l'aveugle. `balanced_percentile_v1` est ajouté
> en shadow 50/50 par venue, amplitude eligibility-only, avec artefact live et
> bench walk-forward. Premier replay : 21 snapshots uniques seulement, résultats
> mixtes selon venue/horizon ; statut `insufficient_history`, aucune bascule
> recommandée. Seuil minimal d'examen : 60 snapshots, sans activation automatique.

---

## D10 — Hot-lists par marché : univers actif selon les marchés ouverts  🛠 implémenté (2026-06-15)
**Contexte.** Le hot-set unique de D9 (cap 25, classement global) finit dominé par le marché le plus volatil (constat run réel : 100 % valeurs taïwanaises) et **gaspille des slots quand ce marché est fermé** (les symboles ne tradent pas, mais occupent le budget). Or `universe.yaml` est aussi un **contrat de surveillance** (le daemon `reconcile_universe` purge wakes/watches des symboles absents).
**Décision (Erwan, 2026-06-15).** Passer d'un hot-set global à **une hot-list par venue de marché** (TW, EU, US, + une *sleeve* 24/5 pour FX/crypto). Le daemon compose l'univers réellement tradé à chaque cycle :
`univers_actif = sticky_all ∪ sleeve_24/5 ∪ union(hot-lists des marchés actions OUVERTS à l'instant t)`.
- **Sélection intra-venue** : chaque marché est classé contre lui-même (règle le problème d'échelles de scores entre marchés), recalculé à la **clôture de SA session**.
- **Pas d'allocateur dynamique** : les marchés actions ne se chevauchent quasi pas (cf Données), donc 22h/24 un seul marché actions ouvert ; pendant le seul chevauchement EU+US (~2h/j) on prend l'**union** des deux listes. Le frein de coût vivant côté queue est le triptyque timeout par appel + AIMD + traitement async sur univers borné (pas de cap d'appels). Si l'overlap chauffe → ajouter un `overlap_cap` simple, pas un allocateur.
- **Sticky béton (bloquant résolu)** : `sticky_all` (positions broker + plans armés + exit-watches + ordres pending) est **toujours dans l'univers, hors logique d'ouverture et hors quota**. Une position sur un marché fermé reste surveillée (exit-plan/watch préservés jusqu'à réouverture). On ne lâche jamais un sticky ; le cap ne porte que sur les non-sticky.
- **Sleeve `ALWAYS_ON`** (FX = 24/5, crypto plus tard) : petit cap dédié (2-5), toujours active si data fraîche, refresh par pseudo-clôture quotidienne + pause week-end, **ne remplit jamais les slots actions**.
- **Deux mécanismes nets** : `update_venue_ranking(venue)` à la clôture (met à jour le classement de la venue, sans écrire l'univers) + `compose_active_universe(now)` à chaque cycle (**idempotent** : n'écrit `universe.yaml` que si le contenu change, jamais vide → fallback dernier univers valide/sticky). État **par venue** (un ranking TW ne touche pas l'hystérésis US).
**Données (horaires vérifiés, review Codex avec sources).** UTC été : TWSE/TPEx `01:00-05:30`, Euronext/Xetra `07:00-15:30`, NYSE/Nasdaq `13:30-20:00`. Chevauchements : TW↔EU = **0h**, TW↔US = **0h**, EU↔US = **2h** (`13:30-15:30`, ~3h en décalage DST). FX = **24/5** (pas 24/7).
**Review.** Codex (design) : 10 findings sur l'archi initiale (sticky marché fermé = bloquant ; allocateur ; état par venue ; deux mécanismes ; idempotence ; FX ; bootstrap ; DST). Après vérif horaires, l'allocateur dynamique est **retiré** (over-engineering) au profit de l'union courte EU+US. S'appuie sur D9.
**Fix préalable.** `venue_of()` classe les `.TWO` (TPEx) en US → corriger `.TW`/`.TWO` → TW (le pool contient des `.TWO`) ; durcir `load_sessions(config_dir)`.
**Implémenté (2026-06-15).** `trader/rotation_venues.py` (état par venue : `update_venue_ranking`, `compose_active_universe`, `run_venue_close`, `due_venues`, `tick`) + `rotation_wiring.py` (`venue_of`, `build_rank_fn`, benchmark par venue) + `rotation_schedule.py` (`open_venues`, `closed_sessions_since`). Câblage daemon : `tick()` à chaque cycle avant le reload d'univers (écriture idempotente `universe.yaml`). Validé sur run réel (pool 283, TW seul → EU+US union pendant l'overlap → tout fermé = univers non réécrit). Décisions liées : **FX retiré** (full actions, pool 100 % equities) ; **fix venues EU** dans `session_snapshot` (les suffixes hors `.PA`/`.DE` tombaient sur le calendrier US → faux « marché fermé ») avec test d'invariant anti-divergence ; **vue cockpit** « Univers actif » par marché + score D10.
**Points ouverts.** DST/fériés/demi-séances (MVP en config UTC manuelle documentée ; à terme `timezone` + `valid_from/to` + jours fermés) ; calibration des caps par venue ; mesurer l'overlap EU+US sur 1-2 semaines.

---

## D11 — Stop/TP adaptatifs : intention paramétrique armée, résolue en prix AU TIR  ✅ validé (principe) · 🛠 Phases 1-2 implémentées (2026-06-15)
**Contexte.** Incident 15/06 : deux plans armés (CFR.SW, ASML.AS) déclenchés ~25-30 min après l'open EU, stoppés au prix exact ~20 min plus tard (−45.70€/−49.70€ ; risque ~0,045 % equity chacun, soit des 1R propres). Cause racine confirmée (revue Codex) : le `hard_stop` d'un plan armé `EXECUTE_ORDER` est un **prix absolu figé à l'armement** (`daemon.py:331`, `trade_plan.py:427`, `exit_engine.py:44`) — jamais re-résolu sur données fraîches au tir. Entre armement et tir, la vol bouge : un stop posé ~1.6× la vol calme pré-séance ne vaut plus que ~1.26× la vol au tir → trop serré. L'ordre exécuté EST bien celui du LLM (pas un gabarit système — fact-check Codex C1-C3) ; le problème est l'**early-binding sur prix absolu**.
**Décision (Erwan, validée).** Le stop doit être une **intention paramétrique** armée par le LLM, **résolue en prix absolu au déclenchement** sur données fraîches (late-binding). Hybride, séquencé :
- **Phase 1** (infra existe déjà) : `hard_stop` accepte `volatility_multiple` (+ `percent` en plancher/plafond). `trailing_stop` supporte DÉJÀ `percent`/`volatility_multiple` (`trade_plan.py:16,250`) et `_reference_volatility_for_symbol` existe (`daemon.py:551`) — on étend le pattern au hard_stop d'entrée. TP en `risk_multiple` (R).
- **Phase 2** : ancrage chartiste/structurel (`swing_low`, `range_low`, `vwap`) à définitions fermées, une fois les niveaux extractibles (aujourd'hui `chart_breakout` donne un signal, pas un niveau — `features.py:210`).
- Au tir : resolver pur `resolve_exit_plan_at_trigger(...)` → `resolved_exit_plan` + `resolution_trace` machine-readable, AVANT `armed_order_price_coherent` ; échec = annulation à code enum + réveil planificateur. Le `TradePlan` final reste un prix absolu, mais sa résolution devient déterministe, fraîche, rejouable.
- **Observabilité** : persister la trace de résolution (`spec`, `resolved_price`, `reference_volatility`, `source`, `window`, `created_at`, `triggered_at`, `cancel_code`) — comble le trou actuel (l'ordre armé exécuté n'est consigné nulle part de récupérable).
**Données.** Revue Codex (fact-check C1-C5 + design, lecture seule). C3 VRAI (prix absolu figé) ; C4/C5 NUANCE (`trailing_stop` a déjà `volatility_multiple` ; garde stale existe mais pas de garde séance ; `qty` clampable par risk gate, stop/TP non). Reco design Codex : **B (vol-multiple) + D (re-validation déterministe au tir) + E (garde séance), A (%) en floor/cap, C (structurel) phase 2** — convergente avec l'analyse Claude.
**Esquisse de contrat.** `codex_client.py` : étendre le schéma `exit_plan` (`hard_stop` : `price|percent|volatility_multiple`). `trade_plan.py` : séparer `validate_exit_plan(..., allow_unresolved=True)` (armement) de la validation résolue ; `create_trade_plan` ne reçoit que l'`exit_plan` résolu (garde `exit_engine` simple). `daemon.py` : resolver pur au tir.
**Points ouverts.** Valeur par défaut du multiple + floor/cap % ; source de vol (`volatility`/`ohlc_volatility`/futur ATR) ; TP en R ; plan d'implémentation TDD. **Hypothèse FAIBLE à NE PAS sur-interpréter (n=3, possiblement de la variance — Erwan sceptique) :** les cassures auto-tradées en fenêtre d'ouverture EU pourraient être de moindre qualité (RHM 11/06, CFR/ASML 15/06). NB : le mandat donne `session.since_open_m` mais aucune guidance « open dangereux » ; D3 note que 85 % des moves arrivent en milieu de séance. À confirmer sur échantillon plus large (filtrer l'historique par `since_open_m`) AVANT toute action — ne rien graver tant que ce n'est pas mesuré.
**Implémenté (Phase 1, 2026-06-15).** Chaîne late-binding complète (TDD pair Codex, 4 unités) : `validate_exit_plan(allow_unresolved=)` accepte `hard_stop` relatif (`percent`/`volatility_multiple` + `min_pct`/`max_pct`) et TP `risk_multiple` ; resolver pur `resolve_exit_plan()` → prix + trace ; `daemon.run_cycle` résout au tir sur vol fraîche (`_reference_volatility_for_symbol`) avant `armed_order_price_coherent`, event `armed_plan_resolved`, annulation à code enum (`armed_plan_cancelled:exit_unresolved:*`) si vol indisponible ; `indicator_watch.normalize_armed_order` accepte le relatif (validé à l'armement, import paresseux anti-circulaire) ; contrat `codex_client` annonce le schéma relatif dans la section plans armés et **recommande `volatility_multiple`**. Review Codex indépendante : 5 findings, 4 corrigés (normalisation alias dans le resolver ; specs relatives cantonnées aux plans armés ; validation à l'armement ; garde `take_profit_resolved_non_positive`), 1 différé (enrichissement de la trace d'audit — repris en Phase 2). 1270 passed, 1 skipped. Commit `84ed965`.
**Implémenté (Phase 2, 2026-06-15, commit suivant).** Ancrage CHARTISTE/structurel : `hard_stop` `{type:"structural", anchor:"swing_low"|"swing_high"|"vwap", window, buffer_pct?|buffer_atr?, min_pct?/max_pct?}`, niveau extrait des barres FRAÎCHES au tir et résolu en prix. `trader/features.py` : `swing_low`/`swing_high`/`vwap` (extraction pure). `resolve_exit_plan` : garde « bon côté » sur le niveau PRÉ-buffer (corrigé après review : contournable par le buffer sinon), buffer pct/atr exclusifs, clamp, garde non-positif. Daemon passe `tradable_bars_by_symbol` au tir (`window` en barres du timeframe runtime — `timeframe` retiré du schéma car non câblé, évite une trace mensongère). Trace d'audit enrichie (finding 4 Phase 1 absorbé). Contrat `codex_client` annonce `structural` dans la section plans armés. TDD pair Codex (4 unités) + review Codex indépendante (verdict BLOQUANT : garde bon-côté contournable + `timeframe` non câblé → 2 corrigés). 1296 passed, 1 skipped. **Reste (Phase 2+, optionnel)** : multi-timeframe réel pour le niveau structural ; ancres supplémentaires (range_low/range_high, fractales) ; mesure « open dangereux » (D11 points ouverts).

---

## D12 — Préflight LLM des ouvertures swing planifiées  💬 en discussion (2026-06-17) · ⛔ non retenu pour l'instant (2026-07-02)
**Contexte.** Le passage vers un horizon swing rend utile la planification hors marché :
daily/swing context valide, mais runtime 15m stale ou marché fermé. Le design
`docs/superpowers/specs/2026-06-17-swing-watch-preflight-post-entry-design.md`
sépare donc analyse/veille et exécution. Il introduit aussi une tension avec D7B :
D7B a explicitement validé l'exécution directe des `EXECUTE_ORDER` sans re-appel LLM.
**Décision proposée.** Ne pas réécrire D7B implicitement. Créer un chemin D12
conditionnel pour les plans d'ouverture dont la thèse a pu se périmer :
cross-session / gap d'ouverture, âge d'armement supérieur à un seuil, ou
`requires_preflight=true` explicite. Au déclenchement, si prix runtime frais et
session tradable, le daemon appelle le LLM pour relire la thèse (`CONFIRM` /
`ADJUST` / `CANCEL` / `DEFER`) avant de passer au `RiskGate`. Le `RiskGate`
déterministe reste inchangé et demeure obligatoire avant `broker.submit()`.
**Non-décision.** D12 n'est pas la correction du postmortem CFR/ASML : D11 a déjà
corrigé les stops figés via late-binding. D12 traite une autre faille, l'absence de
veto de thèse au moment du tir.
**Points d'intégration.**
- `WAKE_WITH_ORDER_INTENT` n'est pas aujourd'hui un vrai chemin préflight : il sert
  surtout de dégradation quand le contrat `EXECUTE_ORDER` est invalide. Le câblage
  armement -> préflight -> exécution est un vrai chantier.
- `ARMED_ORDER_MAX_TTL_MINUTES=240` borne les plans armés à 4 h ; les setups swing
  multi-jours demandent un renouvellement explicite, un TTL différent, ou un réveil
  pré-open qui régénère le plan.
- Le suivi post-entry v1 doit étendre le chemin existant : wake court après fill,
  puis ce réveil agent contourne immédiatement le debounce de position. Hors événement,
  `has_position` garantit une revue LLM au plus tard toutes les 2 h. Pas de canal `post_entry_watch` parallèle ;
  si un tel champ existe plus tard hors position, il devra devenir sticky D10.
- `last_llm_review` est persisté dans `TradePlan` puis **réinjecté au contexte LLM**
  par symbole (continuité de thèse au réveil). Il ne sert PAS à réhydrater le cache
  du gate : la cadence utilise `last_llm_at`, distinct du verdict métier. Une
  transition de régime/signal HTF (apparition, inversion, disparition ou réapparition
  après revue de l'absence) contourne les 2 h ; triggers et réveils agent aussi.
**Point ouvert principal.** Critère de déclenchement du préflight : conditionnel
vs global. La recommandation actuelle est conditionnelle pour préserver D7B sur les
plans intra-séance récents, éviter le double-jugement inutile, et mesurer d'abord
les cas cross-session / open gap / plan âgé avec `backtest/plan_replay.py` et
`state/decisions.jsonl`.
**Décision Erwan (2026-07-02) : préflight NON retenu pour l'instant.** Le risque que
D12 cible (absence de veto de thèse au tir) est **empiriquement négligeable à ce jour**.
Vérif du 2026-07-02 (fil d'analyse agent, cross-check Codex « prendre de la hauteur ») :
sur ~2900 décisions, les **exécutions** de plans armés sont RARES (5 sur 07-01/07-02) ;
la seule perte armée documentée = l'incident CFR.SW/ASML.AS (2 trades, −95,40 €) dont la
cause était le **stop figé pré-open, déjà corrigé par D11** (late-binding) — **0 cas propre
de « thèse morte armée qui perd »**. Le postmortem lui-même juge le danger de la fenêtre
d'open « hypothèse FAIBLE (n=3, pertes 1R dans le bruit) ».

Le **filet existant suffit** : au tir, 5 garde-fous mécaniques (conflit, stale/pas de prix,
`position_exists`, `exit_unresolved`, `armed_order_price_coherent`) → tout écart **annule +
réveille le LLM**. Précision de câblage vérifiée : un plan invalidé laisse le symbole dans
`decidable` (`daemon.py:2491` ne retire que ceux qui s'exécutent) → **le LLM re-décide ce
cycle-là** avec le trigger annoté. Seuls les plans mécaniquement nickel s'exécutent sans LLM,
et ils sont rares. Le trou résiduel = uniquement la thèse *mécaniquement cohérente mais morte*
(fakeout/régime/news) — non observé dans les données.

**Réouverture conditionnée à une MESURE**, pas à une intuition : `backtest/plan_replay.py` +
`state/decisions.jsonl` montrant des pertes récurrentes attribuables à une thèse périmée au
tir sur des plans *mécaniquement valides*. Tant que non mesuré, on ne construit ni préflight
ni superviseur de plans (surface/risque/coût LLM en plus sans gain démontré).

**Statut.** En discussion depuis 2026-06-17, **préflight écarté 2026-07-02** (réouvrable sur
mesure). Le code conserve le comportement D7B actuel pour `EXECUTE_ORDER`.

---

## D13 — Univers = surveillance active : rotation swing-aware + réactivation du sélecteur LLM  🛠 implémenté (2026-06-17)
**Contexte.** Mismatch découvert entre deux mécanismes committés, chacun correct seul, qui donnent un sens **opposé** au même `config/universe.yaml`. La **rotation D9/D10** (`rotation_venues.compose_active_universe`) le traite comme « ce qu'on trade maintenant » : `univers = sticky ∪ union(hotlists des marchés OUVERTS)` → éjecte les marchés fermés pour économiser le hot-path 15m. Le **swing briques 0-4** (`daemon.run_cycle`, design swing-watch) le traite comme « ce sur quoi on peut réfléchir » : analyser à froid les fermés daily-valides avant le gong. Le daemon écrit `universe.yaml` AVANT `run_cycle`, donc un marché fermé **sans position** n'entre jamais → le swing ne le voit jamais (seuls les sticky survivent). Second constat : le **sélecteur LLM** voulu par D9 (décision verrouillée n°1) est **dormant** — `radar.yaml override_enabled:true` mais lu seulement par `run_cli` (chemin D9 jamais appelé par le daemon) ; le chemin vivant `rotation_venues.tick` (D10) n'a aucun hook override. Oubli du passage D9→D10. Et son prompt ne recevait jamais les sticky (sélection en aveugle).
**Décision (Erwan, 2026-06-17).** Réétiqueter `universe.yaml` en **univers de surveillance active** (l'exécutabilité reste décidée au runtime par briques 2/3, rien de tradable n'est persisté). Deux volets imbriqués :
- **A — Appartenance swing-aware (déterministe).** `open_venues(now)` → `analyzable_venues(now) = open ∪ preopen` (venues dont la vraie prochaine ouverture est dans `preopen_window_minutes`, défaut **90**). Admettre un fermé est sûr : brique 3 bloque déjà tout `submit` hors séance. À l'ouverture preopen→open **sans churn** ; à la clôture la venue disparaît (sauf sticky) → coût D9/D10 préservé. Calendrier = **même source que `session_snapshot`** (`exchange_calendars`, §13.8).
- **B — Réactivation du sélecteur LLM, défaut déterministe + override tracé.** Câbler l'override sur le chemin **vivant D10** : ranking déterministe à la clôture (**baseline backtestable**, D9 lock n°1 préservé) ; appel LLM override **en pré-open**, 1/venue/jour (~3/jour), avec **sticky** + **régime sectoriel (D2)** dans le prompt (fin du « sélection en aveugle »). `apply_override` protège déjà les sticky ; échec LLM ⇒ **fail-safe** sur le défaut ; `rotation_ledger` mesure défaut-vs-final (alpha).
**Rejeté.** « LLM choisit tout le pool sans shortlist » : pool = **431 symboles** (US 246/EU 133/TW 52) → coût tokens + discrimination faible >~40 items ; la shortlist radar est un **compresseur 431→N** *et* la baseline backtestable, pas une cage. « Deux fichiers univers » (exécution/analyse) : races concrètes (double `reconcile_universe`, sticky dédoublé, watch froide purgée, fichiers désync) — utile seulement avec une vraie passe research séparée, cible ultérieure. « Admettre les fermés en continu » : recrée le coût D9/D10 sans valeur (daily immobile la nuit).
**Garde-fous (Codex).** (1) sticky AVANT que la venue ressorte sinon `reconcile_universe` purge les plans froids ; (2) symbole pré-open doit être **dû** (`due_symbols`) avant l'ouverture ; (3) overlap pré-open borné par timeout + AIMD + async + univers, sans cap d'appels queue ; (4) calendrier source unique.
**Design.** livré (historique dans `git log`).
**Statut.** 🛠 Implémenté 2026-06-17 (TDD, vérif Codex). Phase A `92f8e47` (fenêtre pré-open déterministe : `preopen_venues`/`analyzable_venues`, `tick` admet les fermés daily-valides). Phase B `47af2e6` (sélecteur LLM réactivé sur D10 : `candidates` top 50+scores+bias persistés, override pré-open 1×/venue/jour dans la shortlist, prompt sticky+régime, **baseline `default_hotlist` séparée de l'effectif `hotlist`** → ledger alpha non pollué, fail-safe). 1428 tests verts. `override_enabled` branché sur D10 (plus dormant). **Reste (follow-ups tracés, non bloquants)** : (1) le daemon ne persiste pas encore `last_regime.json` → `market_context=None` en prod v1 (sélecteur sticky-aware mais sans contexte régime tant que ce fichier n'est pas écrit) ; (2) chemin D9 legacy (`run_cli`/`maybe_rotate`) nettoyé le 2026-09-18 (`17e23dc`) ; (3) `scores`/`dwell` décrivent la baseline déterministe (TUI « — » pour un symbole ajouté par override — cohérent avec la séparation). S'appuie sur D9, D10, et le design swing-watch (briques 0-4).

> **Addendum 2026-07-10 :** D15 remplace le contrat `candidates top 50` et
> précise que l'agent univers est le propriétaire décisionnel de la hotlist. Le
> chemin swing-aware, le pré-open et l'encodage `add/remove` restent compatibles.

---

## D14 — Comptabilité en base USD + sizing en devise native (couche FX)  🛠 implémenté (2026-06-24)
**Contexte.** Le système n'avait **aucune conversion de devise**. `cash`, `equity`, `P&L réalisé`, `gross_exposure` et le **sizing** sommaient/comparaient des montants TWD/EUR/USD comme s'ils étaient homogènes. Révélé par le trade Realtek `2379.TW` (23/06) : P&L affiché −570 que l'on croyait en € alors qu'il était en **TWD** (≈ −16,5 €). Pire que l'affichage — la **taille de position** est calculée en USD implicite sur des prix natifs (`risk.py` `max_order_value / price`, et l'agent qui propose la quantité), donc les positions non-USD sortaient **~35× trop petites** → le plancher de commission IBKR Taiwan (80 TWD/ordre) pesait 184 bps aller-retour → perte structurelle. L'univers courant était à 92 % taïwanais (rotation D13), donc le book entier était faussé.
**Décision (Erwan, 2026-06-24).** Base = **USD**. Garder l'univers multi-devises (le problème est la *taille*, pas la *devise* — pas d'amputation, pas de veto frais). **Deux référentiels qui ne se mélangent jamais** :
- **USD = vue humaine + vérité comptable** : cockpit, `cash`, `equity`, `P&L`, bornes de risque, et le *budget* de risque (ancré sur l'equity USD). C'est ce qu'Erwan lit.
- **Natif = vue interne de l'agent** : il analyse, pose ses niveaux ET **dimensionne** par symbole dans la devise de cotation, **sans jamais convertir**. Le code lui livre son budget déjà converti en natif (`risk_budget_native = risk_pct × equity_USD ÷ fx_rate`, `max_order_native`) dans le contexte estampillé `ccy`/`fx_usd`. Le risk gate reste le **fusible** : il **rejette** (jamais ne clampe) un ordre hors bornes, en USD. **L'analyse (bougies, indicateurs, swings) n'est JAMAIS convertie** — le FX y injecterait du bruit. Conversion uniquement au pont sizing (côté code) et à l'affichage cockpit (côté humain).
**Source FX.** Taux yfinance **live cachés par cycle** (paires `TWD=X` invert, `EURUSD=X` direct) avec **fallback statique** `config/fx.yaml` ; `fx_rate` **persisté sur chaque fill** → attribution déterministe (reconvertit au taux du fill, pas du jour).
**Rejeté.** Amputer l'univers à l'USD (jette la diversif TW/EU + rotation ; le sizing correct dissout déjà le problème de frais) ; veto frais / garde-fou min-notionnel (inutile une fois le sizing correct) ; convertir les bougies/indicateurs (corrompt les indicateurs).
**Design/Plan.** livrés (historique dans `git log`).
**Statut.** 🛠 Implémenté 2026-06-24 (SDD, un sous-agent + review spec/qualité par task, fixes re-revus). Branche `feat/fx-conversion-base-usd`. Modules `trader/fx.py` (pur) + `trader/fx_rates.py` (live+fallback) ; `Fill.fx_rate` + cash broker USD ; `RiskGate.check`/`max_quantity_at_risk`/`max_order_quantity_at_price` currency-correct ; câblage daemon (taux du cycle, `_rate`, propagation aux `broker.submit`, reject-not-clamp préservé) ; contexte agent estampillé `ccy`/`fx_usd`/`risk_budget_native`/`max_order_native` + prompt natif ; `attribution` P&L USD via `fx_rate` du fill ; `portfolio` valorise holdings/equity/gross en USD (corrige cockpit + gate + `pnl_pct`) ; cockpit/TUI en `$`, niveaux de prix restent natifs ; script `scripts/migrate_fx_cash.py` (dry-run par défaut, recalcul cash USD depuis les fills). Suite **1575 verts**. **Reste** : migration de l'état réel (dry-run à valider par Erwan avant `--commit`) ; follow-ups review finale (footgun `^FCHI` commission EUR sur indice mappé USD ; `backtest/engine.py` `_gross_exposure` fx-blind hors-scope ; fallback `1.0` silencieux sur devise de commission inconnue).

---

## D15 — Pool candidat news-aware et agent univers propriétaire de la hotlist  🛠 sélection implémentée (2026-07-10)

**Contexte.** D9/D10/D13 ont historiquement fait porter plusieurs rôles aux mêmes
artefacts (`candidates`, `default_hotlist`, `hotlist`, cap de surveillance) :
scope cheap, baseline déterministe, sélection chaude et sticky. Il faut séparer
la largeur d'analyse de la décision finale sans perdre le fallback backtestable.

**Décision (Erwan, 2026-07-10).** Quatre contrats distincts :

1. **Pool candidat par venue** = top 40 du ranking radar éligible + tous les
   challengers fresh-news qualifiés. Aucun cap global ne retranche les
   challengers. Une news contourne seulement l'heuristique top 40, jamais les
   critères radar de données, liquidité ou exclusion. Chaque challenger garde
   score, TTL, provenance et `source_refs`.
2. **Brief analyste** = constat macro/news sourcé sur ce pool. L'analyste ne
   construit pas la shortlist et n'émet aucun ajout/retrait de hotlist.
3. **Hotlist** = au plus 25 symboles non-sticky choisis dans ce pool par
   **l'agent univers, seul propriétaire décisionnel de la sélection chaude**.
   L'encodage runtime actuel en `add/remove` contre une baseline reste une
   compatibilité. La baseline déterministe est la référence de mesure et le
   fallback explicite si l'agent échoue.
4. **Univers actif** = hotlist choisie + tous les sticky, ajoutés ensuite hors
   quota. Le seul statut sticky ne fait pas entrer un symbole dans le pool
   candidat.

**Observabilité.** Le snapshot `venue_state.json` ne suffit pas. Le runtime garde
un ledger scout append-only (`candidate_run_id`, couverture partielle, compteurs
de rejet, challengers et UUID), des scopes candidats immuables, puis un ledger
agent univers séparé (`agent_run_id`, `candidate_scope_id`, `brief_ref`, baseline,
sélection et statut). Une projection préparée exacte est activée au pré-open ; le
ledger rotation garde les sticky et le fallback. Les jointures permettent de suivre
`news -> challenger -> brief -> hotlist -> décision -> outcome` sans recopier le
corpus brut.

**Mémoire de situation.** Le chemin frais n'a pas besoin de RAG.
`situation_memory.db` est aujourd'hui un index FTS5 dérivé des points de briefs,
sans consommateur runtime, sans FLAIR situation actif et sans MemRL
(MAJ 2026-08-16 : FLAIR situation livré depuis 9fa7920, MemRL toujours non
actif — voir D17.). Si le retrieval est promu, son premier consommateur est
l'agent univers : il reçoit des situations historiques analogues comme
contexte, mais le RAG ne construit ni le pool ni la hotlist. `learnings.db`
reste une mémoire de trading distincte.

**Supersession ciblée.** D15 supersède le cap paper 50 et la propriété nominale
`défaut + add/remove` de D9, ainsi que le contrat `candidates top 50` de D13-B et
la proposition de largeur/hotlist adaptative du design du 2026-07-05. Il conserve
le ranking radar déterministe et éligible de D9, les venues de D10,
`analyzable_venues` de D13-A, l'hystérésis, le fallback, le ledger
baseline-versus-final et le principe sticky hors quota. Les décisions historiques
ne sont pas réécrites.

**État d'implémentation.** Le top 40, les challengers, l'analyste async, les
briefs append-only liés au scope, la projection bornée au prompt univers, la
sélection agent async, l'activation exacte pré-open, les ledgers par run, le
fallback explicite et la composition sticky hors quota sont câblés. Le code est
activé par défaut (`CASYS_NEWS_MACRO_ANALYST_ENABLED` et
`CASYS_UNIVERSE_INTELLIGENCE_ENABLED`, valeur `0` pour couper). Un état live
ancien ne matérialise ces artefacts qu'au prochain close puis au prochain
pré-open avec daemon actif.
Restent le mandat enrichi au trader et, éventuellement, le retrieval historique
après mesure. Ni la couverture news globale ni `last_regime.json` ne sont garantis
complets aujourd'hui.

**Cadence corrigée (2026-07-10).** La clôture ne fige plus qu'un parent
quantitatif top 40 (`scope_phase=close`). À T-90, le pré-open crée un enfant final
stable (`scope_phase=preopen`) en fusionnant tous les challengers qualifiés,
notamment les news overnight, puis déclenche analyste et agent univers sur cet
enfant uniquement. À T-15, la projection exacte est activée ; sinon le parent
reste la source de la baseline et le fallback est tracé. La filiation
`parent_candidate_scope_id`/`parent_close_at` est exposée dans les artefacts et le
cockpit. Hystérésis et `dwell` restent exclusivement des responsabilités de
clôture.

**Contexte famille global (2026-07-10).** Les trois agents restent indépendants,
mais reçoivent désormais le même `GlobalFamilyBoard` construit depuis les derniers
scopes pré-open et briefs exacts TW/EU/US. Il compare les rangs radar intra-venue,
la largeur, les challengers et les signaux analyste bornés. Sa doctrine est
encodée dans l'artefact et le prompt : `comparative_context_not_capital_allocation`.
Il n'alloue ni cash, ni slots, ni risque ; chaque agent univers demeure seul
propriétaire de sa hotlist locale. Le board est append-only sur changement
matériel et visible dans le cockpit avec couverture et fraîcheur.

**Design de référence.**
`docs/superpowers/specs/2026-07-09-universe-intelligence-pass-design.md`.

---

## D16 — Sélections univers jugées contre le banc : verdict à deux bases  🛠 implémenté (2026-08-16)

**Contexte.** La boucle d'attribution des sélections d'univers (attribution
`9fbc9ae`, digest → prompt `500cfdd`) ne juge que la promesse directionnelle
(`allowed_sides` strictement long ou short). Mesuré au 2026-08-16 : 8 789
lignes de sélection dans l'historique mandats (476 lignes, prepared + active +
fallback confondus), **22 % directionnelles → 78 % `non_evaluable`**, digest
quasi toujours `insufficient` — la boucle est câblée mais structurellement
muette (le contrat du prompt pousse d'ailleurs `["long","short"]` même quand
`directional_view` est tranché). De plus, 149/476 lignes `status=fallback`
(baseline déterministe) sont jugées comme des choix d'agent, ce qui biaise le
signal.

**Décision (Erwan, 2026-08-16).** L'agent univers est un **allocateur
d'attention** ; on le juge comme un allocateur, pas comme un prédicteur :

1. **`verdict_basis=allocation`**, pour toutes les sélections : opportunité
   (|forward return| 5 séances) du pick comparée à la **médiane du banc** — les
   candidats du scope D15 non retenus, **hors sticky** (présents dans
   `candidates[]` mais interdits à l'agent). Gagnant/perdant selon l'excès,
   bande d'égalité = `SIGNIFICANT_RETURN_BAND`. Aucun seuil absolu de
   réussite : le banc est sa propre baseline, et la comparaison intra-venue
   neutralise les frais. Couverture structurelle : toute sélection devient
   jugeable (résiduel `non_evaluable` si données/banc insuffisants).
2. **`verdict_basis=direction`**, inchangé : les sélections directionnelles
   restent jugées par le juge signé existant, déjà mutualisé à l'étage domaine
   (`domain/learnings/scoring.py`) avec learnings et notes de situation.
3. **FLAIR par base** (base rates séparés). Le base rate allocation ≈ 0,5 pour
   un sélecteur aléatoire → le lift mesure directement le talent d'allocation.
4. **`selector` agent vs baseline_fallback** séparés : le digest injecté au
   prompt ne porte que sur l'agent ; le comparatif agent vs baseline
   déterministe devient une requête analytics gratuite.
5. **Sémantique versionnée** (pattern `outcome_semantics_version`, table
   metadata dédiée dans casys.db) avec purge et **rejugement complet** —
   possible car scopes persistés depuis le 2026-07-10 et mandats depuis le
   2026-07-17.

**Données.** 476/476 lignes de mandat portent `candidate_scope_id` ; scopes
candidats immuables (~40 candidats/venue, top 40 radar) dans
`state/candidate_scopes/` — le contrefactuel est déjà persisté, zéro mécanisme
de capture à ajouter (dividende direct de l'observabilité décidée en D15).

**Fact-check (grok-4.6, 2026-08-16) : GO-AVEC-CORRECTIFS**, intégrés à la
spec : unité de jugement = activations seules (`active|fallback`, `prepared`
exclu, rafales de fallback dédupliquées) ; sticky exclus du banc ; lecture de
scope **par id** à créer (l'API actuelle ne lit que le dernier par venue) ;
migration v7 par recréation de table (UNIQUE table-level non altérable en
SQLite) ; horizons immatures jamais persistés (pending, sinon figés à jamais) ;
métadonnée de sémantique dans casys.db.

**Points ouverts.** `MIN_BENCH_EVALUATED` (8) à confirmer à la mesure ;
dénominateurs réels (allocations distinctes après dédup) à publier ; inciter ou
non l'agent à des `allowed_sides` directionnels quand sa vue est tranchée
(décision séparée) ; juger la `default_hotlist` comme baseline virtuelle
(option) ; inscrire `state/candidate_scopes/` dans la politique de rétention
(le rejugement en dépend).

**Design de référence.**
`docs/superpowers/specs/2026-08-16-universe-selection-bench-verdict-design.md`.

**État d'implémentation (2026-08-16).** Lots 1-6 livrés : juge allocation contre
le banc (médiane, bande d'égalité, sticky exclus), unité de jugement
`active|fallback` (prepared exclu, rafales dédupliquées), lecture de scope par
id, store v7 (`bench_v3`) à clé unique élargie, refresh à deux bases avec
pending d'horizon immature, digest deux blocs (allocation / direction,
`selector=agent` seulement) et commande analytics `bench` (agent vs baseline).
Le passage `bench_v2` → `bench_v3` purge les verdicts dérivés puis les rejoue :
un banc assez large mais incomplet ou immature reste désormais pending ; un
scope historique introuvable ou un banc éligible structurellement sous le
plancher reste terminal. Un cursor circulaire durable évite d'affamer les
retryables au-delà du batch. Le refresh n'upsert que les bases nouvelles et ne
révise donc plus une direction déjà persistée en attendant son allocation.
Le digest allocation n'entre dans le prompt univers qu'une fois
`min_n=5` atteint. Grain venue d'abord (picks agent vs banc, et
`vs_baseline` si la hotlist déterministe a aussi n≥5) ; familles/rôles
ensuite. Les FLAIR agent et baseline restent des pools séparés.

---

## D17 — Boucles de feedback fermées : le marché note la mémoire, les sélections et les briefs  🛠 implémenté (rétrospectif, 2026-08-16)

**Contexte.** Cinq commits du 15/08 ont fermé (ou rendu mesurables) des
boucles de feedback sans entrée de registre. Avant eux : `last_llm_at`
volatile (D7, restart = pic `periodic_review`) ; `since_open_m` calculé
mais absent des `decisions.jsonl` (D11, hypothèse « open EU » injugeable) ;
mandats univers jamais scorés ; 2 347 notes de situation à `outcome_score`
figé 0,0 ; FLAIR/MemRL trader déjà en store (`learnings.db`) mais
`citation_utility` et digest univers absents des prompts. Le marché peut
désormais noter la mémoire, les sélections et les briefs — une partie
seulement reboucle vers un agent.

**Décision (constat d'implémentation, pas une nouvelle option).** Le marché
juge l'artefact (sélection, note, règle citée, décision trader), pas
l'agent d'aval. Cinq couches, du plus instrumenté au plus fermé :

1. **Fenêtre de séance — `4cab3ea`.** Mesure : `since_open_m` / `to_close_m`
   (+ `venue` si le snapshot en porte) dans
   `market_snapshot` de chaque ligne `decisions.jsonl`, y compris
   `armed_plan`. `None` reste légitime (hors séance, calendrier KO) et
   n'est jamais remplacé par 0. Atterrissage : ledger uniquement
   (`decision_ledger_rows.py`). **Instrumentation pure** : aucun gate, aucun
   prompt. Rend enfin mesurable la condition de réouverture D11 (« fakeout
   open EU », n=3, à filtrer avant toute garde).

2. **Attribution des sélections — `9fbc9ae`.** Mesure : verdict
   directionnel sur le **mouvement du symbole** (pas sur ce que le trader
   en a fait) — `allowed_sides` long→BUY / short→SELL, juge partagé
   `classify_decision_quality`, horizon 5 séances. Bidirectionnel ou vide
   = `non_evaluable` (limite assumée ~78 % de l'historique). FLAIR
   (`compute_outcome_scores`, shrinkage k=5) persisté dans
   `casys.db.universe_selection_outcomes`. `mandate_ref` promu à la
   **racine** des lignes de décision (déjà collecté, inexploitable en
   requête tant qu'il restait dans `decision.*`). Script
   `scripts/universe_selection_analytics.py`. **Ce lot ne parlait pas à
   l'agent** (prompt univers inchangé). **Supersédé partiellement par D16**
   (même jour, principe validé) : le verdict contre le banc
   (`verdict_basis=allocation`) remplace le juge signé comme jugement
   principal ; la base `direction` de 9fbc9ae est conservée, pas
   abandonnée.

3. **Notes de situation — `9fa7920`.** Mesure : chaque note (direction +
   cible + horizon) contre le marché, pas le portefeuille. Parseur
   d'horizon texte libre → séances cash (plafond 126 ; absent / illisible
   = `non_evaluable`, **pas** de défaut 5 séances). Famille = panier
   équipondéré du **catalogue**, pas l'univers du jour ; zones hors
   périmètre. Atterrissage : colonnes d'outcome + `outcome_score` FLAIR
   dans `situation_memory.db` (`situation_notes`) ; `q_value` reste
   `NULL`. **Scoring live via le sync daemon** (`refresh_situation_outcomes`,
   fail-open) ; le script `situation_note_analytics.py evaluate` reste un
   secours opérateur. Référence : `docs/reference/situation-memory.md`.
   MemRL situation non implémenté (pas de retrieval ⇒ pas de reward à
   apprendre).

4. **Fermeture FLAIR/MemRL — `500cfdd`.** Trois réinjections prompt, plus
   le socle qui les rend stables :
   - **Trader / `recent_decisions` + `last_llm_review`** : feedback FLAIR
     (`status`, `verdict`, `forward_return`, `outcome_score`) annoté depuis
     `learnings.db` par `decision_id` — le marché note les décisions
     récentes dans le cockpit poussé.
   - **Trader / règles globales** : `citation_utility`
     (`helps`/`hurts`) attaché au `global` si `q_updates ≥ 10` (indépendant
     de `robustness` FLAIR). En dessous du seuil : champ omis (`unknown`
     n'est pas poussé).
   - **Univers / `selection_feedback`** : digest comparatif
     familles/rôles (min_n=5, `pays`/`decoit` ignorent les petits
     groupes), rôle
     `comparative_context_not_hotlist`. Poussé seulement si
     `status=observed` ; sinon le prompt n'en voit rien.
   - **Socle consolidateur** : snapshot pending avant l'appel LLM, grâce
     3 jours, conservation des règles mesurées (`q_updates≥10`),
     réutilisation d'ID si le texte normalisé est identique — sans ça le
     MemRL des citations se réinitialisait à chaque leftover.
   - Le sync FLAIR daemon (`learnings_sync_runtime`) évalue aussi les
     mandats (plus seulement le script). Collateral : briefs
     macro/news et micro société rédigés en anglais.

5. **Cadence LLM — `beb9e26`, contexte durable complété le 2026-08-20.** Ferme le backlog D7 « persister
   `last_llm_at` (volatile → batch global à chaque restart) ». Table
   `llm_gate_last_seen` (casys.db, migration v5) ; la migration v8 ajoute
   `wake_reasons_json` et `wake_fingerprints_json`. Timestamp, raisons et
   empreintes sont upsertés atomiquement après chaque revue réelle, puis les
   trois sont hydratés au boot. Une ligne legacy ou un contexte corrompu est
   ignorée en bloc (fail-open : nouvelle revue, jamais faux debounce).
   Backend ≠ sqlite → mémoire seule préservée.
   Ce n'est pas une boucle marché : c'est la persistance du gate de
   pertinence.

**Ce qui reboucle vs ce qui reste mort.** Rebouclent : outcomes trader →
`recent_decisions`/`last_llm_review` ; MemRL des citations →
`citation_utility` (seuil 10) ; FLAIR sélections D16 → digest univers
(min_n=5, agent only, deux bases) ; notes de situation →
`situation_feedback` dans le prompt analyste (min_n=5). Restent de
l'instrumentation : fenêtre de séance (`since_open_m` pas encore analysé
pour D11) ; retrieval FTS situation (aucun agent n'appelle `search()`) ;
MemRL situation (`q_value`).

**Données.** 11 exécutions `armed_plan` réelles sans `since_open_m` avant
4cab3ea. `mandate_ref` déjà présent dans 1 532 / 2 653 lignes, mais
enterré. 9fbc9ae : ~78 % `non_evaluable` (reprise chiffrée en D16 : 8 789
lignes, 22 % directionnelles). 9fa7920 : 2 184 / 2 347 horizons parsés ;
1 016 notes structurellement scorables, 79 matures au 15/08. Suites
vertes au fil des commits (4 276 → 4 326).

**Points ouverts.** `since_open_m` persisté mais **non analysé** (mesure
D11 pas commencée). Retrieval FTS situation toujours inactif. MemRL
situation (`q_value`) jamais scorée.
Le digest `direction` univers ne parle qu'une fois `min_n=5` atteint par
famille × venue.

---

## D18 — Évaluation déterministe obligatoire des entrées  🛠 implémenté (2026-08-18)
**Contexte.** Le LLM pouvait auparavant produire un `strategy_entry` avant de
voir le sizing résolu, les frais et l'économie nette du plan. La confidence gate
scalaire est désactivée dans le profil paper et ne remplace pas cette preuve.

**Décision.** Toute augmentation d'exposition (`OPEN_LONG`, `OPEN_SHORT`,
`SCALE_IN`, jambe ouvrante d'un `FLIP`) doit référencer une
`TradePlanEvaluation` du snapshot courant. L'agent appelle
`evaluate_trade_plan`, observe sizing, stop/cibles, frais, perte/gain, R:R,
`p_break_even`, EV et verdicts des gates, puis recopie l'`evaluation_id`. Le
daemon revérifie l'empreinte et rejoue les gates avant le broker. Les plans armés
portent la référence à l'armement et sont réévalués sur données fraîches au
trigger.

**Politique.** L'évaluation est obligatoire ; son économie reste advisory.
`economics.status=negative|unknown` ne crée pas de hard gate. Seules absence,
péremption, incohérence ou invalidité technique de la référence bloquent ce
préflight. `risk_admission` et le RiskGate restent les autorités d'allocation et
d'exécution. La confidence gate demeure un mécanisme indépendant, actuellement
désactivé par `config/risk.yaml`.

**Calibration.** `confidence` est la probabilité conditionnelle que le
round-trip finisse net positif avant invalidation/horizon. Le read model joint
fills et décisions par `decision_id`, conserve les dimensions d'entrée sans
look-ahead (`unknown` pour l'historique incomplet), et expose cohortes, gap de
calibration, P&L et incertitude. Seul un digest borné est poussé ; le détail est
pull-only via `get_attribution{scope:"calibration"}`.

**Points ouverts.** Un hard gate EV reste explicitement hors périmètre. Toute
activation future demanderait une décision métier séparée et des seuils
configurés, pas une réinterprétation silencieuse de D18.

---

## D19 — World Model shadow isolé, sans autorité Trader  🛠 implémenté (2026-08-22)
**Contexte.** Un critic de dynamique de marché a été tenté via des reconstructions
Brain–Univers (trajectoires, mémoires, prochaine tâche due). Cela mélangeait
action, sélection d'univers et transition de prix, et invitait à lire un
« uplift Trader » dans une coïncidence de PnL.

**Décision.** Le World Model est un bounded context **marché**, shadow-only,
permanent `NO_GO`. Il estime `DOWN` / `FLAT` / `UP` (bande 50 bp) à horizons
fixes `elapsed_4h.v1` et `elapsed_1d.v1` depuis un `WorldEpisode` action-free.
Sans ancre OHLCV valide : pas d'épisode. `anchor_end_at` démarre la transition.
Aucun fallback d'horizon. Preuve append-only (replay exact = no-op, correction =
supersession, jamais d'overwrite). `predicted_at` = cutoff logique ;
`ready_at` = disponibilité réelle de persistance. Baseline Markov et GRU en
ligne (encodeur partagé, le GRU n'importe pas le baseline). Comparaison
appariée, support minimum 20. Drawdown = proxy de marché non-portefeuille.
`actual_contribution=not_attributable` et
`counterfactual_contribution=not_available` tant qu'il n'existe pas de liens
pré-décision append-only **et** une politique d'ordres/fills shadow
pré-enregistrée. Store dédié `state/world_model.db`, indépendant de `casys.db`.
Flag `CASYS_WORLD_MODEL_SHADOW_ENABLED` lu au boot seulement. Live `not_started`
si le daemon n'a pas créé la base.

**Owners.** `domain/world_episode` (contrat) ;
`application/world_model` (capture, labels, modèles, `WorldModelService`) ;
`infrastructure/state_db` (store append-only + query lecture seule) ;
`reporting/read_models` (évaluation, impact, status) ; `runtime` (adapter
background fail-open) ; CLI mince sans SQL.

**Politique.** Jamais d'autorité sur Brain, Univers, scheduler, RiskGate,
broker ou portefeuille. Pas de claim d'uplift Trader. Un changement de flag
n'a d'effet qu'après redémarrage du daemon.

**Points ouverts.** Jointure pré-décision durable et politique d'exécution
shadow pré-enregistrée : hors périmètre jusqu'à une décision métier séparée.
