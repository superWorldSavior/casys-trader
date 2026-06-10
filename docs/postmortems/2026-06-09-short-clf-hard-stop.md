# Post-mortem — short CL=F du 2026-06-09, −101,20 $ (hard_stop)

**Statut : CORRIGÉ** — gate de confiance×risque livré le 2026-06-10
(`trader/risk.py`, commit `dffb6d1`).
**Toute statistique incluant des trades antérieurs au 2026-06-10 mélange deux
régimes** : avant cette date, aucune confiance minimale n'était exigée pour
exécuter. Filtrer par `code_version.git_commit` (présent dans chaque entrée de
`state/decisions.jsonl`) ou par date pour comparer ce qui est comparable.

## Le trade

| | |
|---|---|
| Entrée | 2026-06-09 16:09:45 UTC — SELL (OPEN_SHORT) 115 × CL=F @ 86.71 |
| Confiance | **0.58** — la plus basse de tous les trades exécutés à date |
| Stop | 87.30 (distance 0.59, risque planifié 0.068 % du capital) |
| Sortie | 16:48:41 UTC — exit engine, `hard_stop`, fill @ **87.59** |
| P&L | **−101,20 $** (vs ~−68 $ planifiés : +49 % de slippage) |

## Ce qui s'est passé

1. **12:00→16:00 UTC** : CL=F chute de 89.6 à 86.7 (−3,2 %). L'agent refuse
   8 fois d'entrer (HOLD, confiance jusqu'à 0.97) : « z extrême mais pas de
   confirmation HTF » — discipline conforme à ses learnings.
2. **16:09** : réveil sur indicator_watch (|z| ≥ 2). Volte-face : il **shorte
   l'extrême baissier** (z=−2.86, quasi plus bas du jour à 0.75 $ près) en
   chassant le breakout_down, avec une confiance de 0.58 qu'il qualifie
   lui-même de « risque de continuation » incertain.
3. **16:21** : +75 $ latents (+1,1R). Aucun take-profit partiel exécuté
   (la rationale promettait des « objectifs partiels »).
4. **~16:30-16:45** : V-reversal violent (barre 15m avec high à 89.49).
   Le prix traverse le stop entre deux checks de l'exit engine (granularité
   ~70 s + fraîcheur des barres) → fill à 87.59 au lieu de 87.30.

## Causes racines

1. **Jugement (cause principale)** : trade contradictoire avec les 8 HOLDs
   précédents — shorter un z-score extrême négatif, c'est exactement le
   mean-reversion inversé que ses propres learnings interdisaient. Sa
   confiance basse (0.58) le signalait ; rien ne l'exploitait. La calibration
   forward le confirme : **0 % de win rate sous 0.7 de confiance** (vs 50 %
   au-dessus) — `python -m trader.attribution`.
2. **Slippage structurel** : l'exit engine évalue les stops au prix courant à
   chaque cycle (~70 s), pas en intra-bar. Sur un spike, la perte réelle
   dépasse le risque planifié. Aggravant **confirmé au niveau code** : le
   daemon tourne en données IB **différées** — `connect_ib` a
   `market_data_type=3` (delayed) par défaut (`trader/tools/ib_source.py:374`)
   et le daemon ne l'override pas (`trader/daemon.py:1487`). Cohérent avec
   les marks d'equity du daemon à ~86.5 à 16:47 quand Yahoo affichait ~88
   (~15 min de retard). Sans souscription market data, le runtime décide et
   coupe ses stops sur le passé.

## Le fix (cause 1)

`RiskGate.check_confidence` (`trader/risk.py`) : confiance minimale requise
croissant linéairement avec le risque planifié —
`requise = 0.7 + (0.9 − 0.7) × (risque_planifié / risque_max)` ; pas de stop
⇒ 0.9 exigé ; confiance absente ⇒ rejet fail-safe. Rejet machine-readable
`risk:confidence_below_required`. Paramètres : `config/risk.yaml`
(`min_trade_confidence`, `full_risk_confidence`).

**Ce trade précis est le cas de régression testé** :
`tests/test_risk.py::test_check_confidence_rejette_le_trade_cl_f_du_9_juin`
(conf 0.58, risque 0.068 % → requis ≈ 0.71 → rejeté).

## Suites données (cause 2) — tout est traité au 2026-06-10

- **Données différées** : décision Erwan — pas d'abonnement en paper.
  **Futures CME retirés de l'univers de calibration** (commit `70ad1b9`),
  retour en prod avec souscription IB temps réel. Spec data multi-sources :
  `docs/superpowers/specs/2026-06-10-data-source-abstraction-design.md`.
- **Slippage des stops : CORRIGÉ** (commit `5ce1746`) — stops/TP/trailing
  détectés sur les extrêmes (high/low) de la dernière barre, fill
  conservateur jamais meilleur que le niveau, garde temporelle (un extrême
  antérieur à l'ouverture du plan ne déclenche pas).
- **Plans persistés** (même commit) : trade plan complet dans la décision à
  la création + état/fill à chaque exit — le « TPs introuvables » de ce
  post-mortem ne peut plus se reproduire.
