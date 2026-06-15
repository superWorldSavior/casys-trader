# Post-mortem — CFR.SW + ASML.AS du 2026-06-15, −95,40 € (hard_stop figé, open EU)

**Statut : CORRIGÉ** — D11 Phases 1 & 2 livrées (commits `84ed965`, `c80eb6d`),
mergées sur `main` le 2026-06-15. Stops des plans armés désormais résolus en prix
**au déclenchement sur données fraîches** (late-binding), plus de prix absolu figé
à l'armement.
**Toute statistique mélangeant des trades d'avant/après le go-live mélange deux
régimes de stop** : avant (figé) vs après (résolu au tir). D11 est live depuis
**09:22 UTC** (Phase 1) / **09:52 UTC** (Phase 2). Filtrer par
`code_version.git_commit` (`84ed965`/`c80eb6d`) ou par heure dans
`state/decisions.jsonl`.

## Les trades

| | CFR.SW (Richemont) | ASML.AS (ASML) |
|---|---|---|
| Armement | 07:23:46 UTC — watch `EXECUTE_ORDER` | 07:23:46 UTC — watch `EXECUTE_ORDER` |
| Déclenchement | 07:25:11 → BUY 50 @ **184.40** | 07:28:53 → BUY 5 @ **1649.60** |
| Confiance | 0.73 | 0.74 |
| Stop (figé à l'armement) | 183.50 (0.90 pt = **0.84× vol** au tir) | 1639.80 (9.80 pt = **1.26× vol** au tir) |
| Risque planifié | 0.045 % du capital | 0.049 % du capital |
| Sortie | 07:46:45 — `hard_stop` @ **183.50** | 07:52:50 — `hard_stop` @ **1639.80** |
| P&L net | **−45,70 €** (−0.49 %) | **−49,70 €** (−0.59 %) |

Les 2 pires trades des 17 depuis le 08/06. Stop touché **au tick près** dans les
deux cas, `filled_take_profits: []` → TP1 (~+1.5 %/+1.7 %) jamais approché.

## Ce qui s'est passé (identique pour les deux)

1. **07:21** — cassure 15m forte (chart_breakout, ER, AC). Le LLM dit **HOLD** :
   *« marché ferme, pas d'ordre hors séance, veille à l'ouverture/flux frais »*
   → arme une veille `WAKE`.
2. **07:23** — réévalue, **encore HOLD** : *« séance indiquée fermée ; je n'entre
   pas sur données non négociables »*… **mais** sur-classe la veille en
   `EXECUTE_ORDER` avec un `order` complet (qty, confidence, `exit_plan` à
   `hard_stop` en **prix absolu**).
3. **07:25 / 07:28** — la cassure tient à l'ouverture → **achat auto, sans
   re-appel LLM**. L'ordre exécuté EST celui de l'agent (pas un gabarit système).
4. **07:46 / 07:52** — les deux partent à contre-sens et déroulent jusqu'au
   hard_stop. Pertes 1R propres.

## Causes racines

1. **Stop en PRIX ABSOLU figé à l'armement (cause principale).** Le LLM a posé un
   stop chiffré à 07:23, dimensionné ~1.6× la vol *calme d'avant-séance* (sa
   prose : *« stop sous ~1.6 vol récente »* pour ASML). À l'ouverture, la vol a
   gonflé : le même prix ne valait plus que **1.26× la vol du moment** (ASML) /
   **0.84× vol** (CFR). Un stop figé pré-open se fait mécaniquement courir par la
   volatilité d'ouverture. L'ordre n'a pas changé entre armement et tir — c'est
   le **régime sous l'ordre** qui a changé, et rien ne re-calibrait le stop sur
   la donnée fraîche au déclenchement.
2. **Garde de fraîcheur absente au tir.** Le LLM a armé *justement parce qu'il
   jugeait les données non fraîches* (« séance fermée / prix non actif »),
   s'attendant à un tir « quand ce sera frais ». Mais le trigger n'avait aucune
   condition de fraîcheur/séance, `stale_market_data` était `null`, et le
   scheduler tournait le cycle EU complet à 07:21 → **3 signaux de séance
   incohérents**, zéro re-vérification au tir.
3. **Pas de cut anticipé sur les trades armés.** Contraste révélateur : RHM.DE
   (11/06), même cassure d'open EU mais en **entrée LLM directe**, a été **coupé
   à −0.28 % (≈ break-even) en 16 min** quand le LLM a vu la cassure échouer
   (z<2). Les plans armés n'ont pas cette réévaluation → plein stop.

## Correction d'intuition (à ne PAS sur-interpréter)

Ce **n'étaient pas des break-even** : plein stop 1R chacun. Le souvenir
« break-even début de séance » correspond à RHM.DE, pas à CFR/ASML. **Le danger
de la fenêtre d'ouverture est une hypothèse FAIBLE** (n=3, chaque perte = 1R
propre à ~0.045 % du capital → 2-3 stops d'affilée sont **dans le bruit
normal**) — possiblement de la variance, le modèle le sait peut-être déjà. Non
gravé : à mesurer (filtrer l'historique par `session.since_open_m`) avant toute
action. La lecture sobre : **stop trop serré (réel, corrigé) + un peu de
malchance**.

## Le fix — D11 (décision `docs/decisions/registre-decisions-metier.md`)

**Principe** : le stop d'un plan armé devient une **intention paramétrique**,
**résolue en prix absolu au déclenchement sur vol/barres fraîches** (late-binding).

- **Phase 1** (`84ed965`) — `hard_stop` relatif `volatility_multiple`/`percent`
  + floor/cap `min_pct`/`max_pct` ; TP en `risk_multiple` (R). Resolver pur
  `trade_plan.resolve_exit_plan()` → prix + trace machine-readable. Câblage
  `daemon.run_cycle` : résolution au tir via `_reference_volatility_for_symbol`
  (vol fraîche) avant `armed_order_price_coherent`, event **`armed_plan_resolved`**
  (audit), annulation à code enum `armed_plan_cancelled:exit_unresolved:*` si vol
  indisponible. Contrat `codex_client` : recommande le relatif pour les plans
  armés.
- **Phase 2** (`c80eb6d`) — `hard_stop` **chartiste** `{type:"structural",
  anchor:"swing_low"|"swing_high"|"vwap", window, buffer_pct?|buffer_atr?}` ;
  niveaux extraits des barres fraîches (`trader/features.py`), garde « bon côté »
  sur le niveau **pré-buffer**, clamp, garde non-positif.

**Qualité** : TDD pair Codex (8 unités), 2 reviews Codex indépendantes (Phase 1 :
5 findings, 4 corrigés ; Phase 2 : verdict BLOQUANT — garde bon-côté contournable
par le buffer — corrigé). **1296 passed, 1 skipped.**

## Adoption (au 2026-06-15 ~10:30 UTC)

Le LLM a basculé sur le nouveau schéma **dans les ~40 min** suivant le go-live :
**11/11 plans actuellement armés utilisent `volatility_multiple`** (avec floor/cap),
**0 en prix absolu**. Il choisit la baseline `volatility_multiple` (recommandée),
pas encore `structural`. **Aucun n'a encore déclenché** (0 `armed_plan_resolved`,
dernier fill 07:52) → **résultats pas encore mesurables** : l'adoption est prouvée,
la performance des nouveaux stops reste à valider sur trades clôturés.

## Reste à faire (optionnel)

- Mesurer outcomes : P&L et taux de stop-out, **trades à stop résolu vs figé**
  (filtre `code_version`), une fois quelques round-trips clôturés.
- Mesurer l'hypothèse « open dangereux » par `since_open_m` avant d'agir dessus.
- Phase 2+ : multi-timeframe réel pour le niveau structural ; ancres
  supplémentaires (range, fractales) ; enrichir encore la trace (`cancel_code`).
