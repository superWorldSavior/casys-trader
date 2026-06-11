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
