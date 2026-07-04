# Décisions métier — casys-trader

> Ce document synthétise la logique métier et les décisions de design du système.
> **Généré par analyse du code et du registre — à re-vérifier si l'architecture évolue.**
> Source de vérité pour les décisions : `docs/decisions/registre-decisions-metier.md`.

---

## 1. Philosophie

### 1.1 Planificateur, pas opérateur continu

Le LLM n'est pas un trader « actif » qui regarde le marché en continu. Il est un
**planificateur** : il conçoit des scénarios d'entrée armés (conditions + sens + taille
+ stop) que le daemon exécute mécaniquement au déclenchement — sans re-appel modèle.
Une exception est en discussion avec D12 pour les ouvertures swing planifiées, mais
elle n'est pas validée ni implémentée.

Conséquences :
- Les réveils LLM sont rares et événementiels (position ouverte, trigger, régime fort).
  `relevance_gate.py` filtre le bruit (~99 % de HOLD sur polling pur → évités).
- Le LLM produit des artefacts vérifiables (`indicator_watch`, `exit_plan`), pas des
  intentions en prose — format machine-validable, rejouable, auditable.
- Le gate de risque déterministe (`RiskGate`) reste le **seul fusible** à l'exécution,
  même pour les plans armés.

### 1.2 Sessions stateless

Chaque appel LLM est une session jetable (`acpx exec`). L'état évolutif de l'agent
est externalisé : `mandate/mandate.md` (boucle humaine) + `state/learnings.jsonl`
(machine). L'agent ne se souvient de rien entre deux cycles sauf ce qui lui est
réinjecté dans le contexte.

### 1.3 Faits calculés par le code, pas déduits par le LLM

Le code calcule et injecte : âge des données (`data_age_m`), état de la séance
(`session`), indicateurs techniques, biais de régime, attributions P&L. Le LLM
reçoit des faits bruts et décide — il ne calcule pas. Principe AX « faits injectés
par le code, prose minimale ».

### 1.4 Minimiser les réveils LLM

Cible : ~20 appels LLM / 24 h (contre ~92 avant D7). Leviers :
- D7A — gate de pertinence en code (quiet_gate sur polling calme)
- D7B — plans armés exécutés sans re-appel
- Batch 1-appel/cycle (tous symboles dus en un prompt partagé)

---

## 2. Gates

### 2.1 Fraîcheur des données

Garde « marché live » : la dernière barre 15m doit dater de moins de 40 min
(`DEFAULT_MAX_MARKET_DATA_AGE_MINUTES = 40`, `daemon.py:86`). Dépassé →
`stale_market_data` → exclusion du tradable + backoff exponentiel du réveil
(2× par défaut, cap 120 min). Un symbole stale ne reçoit jamais d'ordre.

### 2.2 Gate de confiance adaptatif au risque

`RiskGate.check_confidence()` (`risk.py:125`) :

```
required = min_trade_confidence + (full_risk_confidence − min_trade_confidence)
           × clamp(planned_risk_pct / max_risk_per_trade_pct, 0, 1)
```

Défauts : `min_trade_confidence = 0.7`, `full_risk_confidence = 0.9`,
`max_risk_per_trade_pct = 1 %`.

Risque non borné (pas de stop) → `full_risk_confidence` exigée.
Le gate ne s'applique qu'aux ouvertures pures (`OPEN_LONG`/`OPEN_SHORT`).
`REDUCE`/`CLOSE` restent toujours possibles (réduire le risque ne doit jamais
être bloqué).

### 2.3 Garde missing_hard_stop

Toute ouverture (`OPEN_LONG`/`OPEN_SHORT`) sans `hard_stop` est **rejetée**,
quelle que soit la confiance. Guardrail humain gravé dans `mandate/guardrails.json`
et enforced en code `daemon.py:1944`. REVERSE : tracé mais non bloqué par ce garde
(**backlog à corriger** — pas un choix intentionnel, cf. §8).

### 2.4 Garde wrong-side stop

Si le `hard_stop` est du mauvais côté du prix d'entrée → rejet
`invalid_exit_plan:hard_stop_wrong_side`. `daemon.py:1886`

### 2.5 Garde données stale au déclenchement (plans armés)

Au déclenchement d'un `EXECUTE_ORDER` : si le symbole est `stale` → annulation
`armed_plan_cancelled:stale` + réveil planificateur (jamais d'exécution en aveugle).
`daemon.py:1544`

### 2.6 RiskGate — limites nominales

`risk.yaml` : `max_position_value`, `max_gross_exposure`, `max_order_value`,
`max_orders_per_cycle`, `min_equity`. Tout ordre dépassant ces bornes est rejeté
ou clampé (`order_value_exceeded` → clamp sur `max_order_value` pour les ouvertures).

---

## 3. Décisions D1..D12 — synthèse

Source de vérité : `docs/decisions/registre-decisions-metier.md`.

| # | Titre | Statut | Intention business |
|---|---|---|---|
| **D1** | Métrique audit `missed` ≠ `bad` | 🛠 implémenté | Sépare les opportunités ratées (HOLD pendant un move) des vrais trades perdants. `bad% réel = 1 %`. |
| **D2** | Biais régime cross-asset par famille thématique | 🛠 implémenté | Le code calcule la synthèse directionnelle par univers (défense, énergie, semis…) et l'injecte comme **signal d'opportunité**, pas un garde-fou. `trader/family_regime.py` |
| **D3** | Réveil sur cluster d'univers | ✅ validé (principe) | Quand ≥ 2 symboles d'un même univers bougent ensemble → réveiller l'agent hors planning. Non encore implémenté. |
| **D4** | RVOL + ATR expansion au cockpit | ✅ validé | Injecter par symbole : RVOL et ATR expansion — signatures à edge (+0,27 %/trade, 55 % win). Non encore implémenté. |
| **D5** | Flux news + LLM qualifiant la surprise | 💬 en discussion | ~80 % des moves sans signal technique préalable = news/macro. Flux temps réel (Polygon/Benzinga) + LLM. Coût/latence à arbitrer. |
| **D6** | Boucle de learnings auto-renforçante | 🛠 étapes 1-2 | Guardrails humains séparés (`mandate/guardrails.json`). Prompt consolidation : cap ≤ 5 abstentions, priorité entrées, seuil robustesse ≥ 3 occurrences. Les bruts ne sont plus réinjectés quand un consolidé existe. |
| **D7** | LLM planificateur actif | 🛠 étages A+B | **A** — gate de pertinence code (`relevance_gate.py`). **B** — plans armés `EXECUTE_ORDER` exécutés sans LLM. Cible : ~10-20 appels/jour. |
| **D8** | Backtest deux étages : replay mécanique + forward | ✅ validé | Étage 1 = mesure a posteriori des plans armés (taux déclenchement, P&L, stops). Étage 2 = qualité des scénarios en forward (attend données réelles). |
| **D9** | Veille deux niveaux : radar + rotation hot-set | 🛠 implémenté | Tier 1 radar daily 0-LLM (~283 symboles). Tier 2 hot-path 15m sur hot-set dynamique (cap M=25, par venue). Score = efficacité_tendance × force_relative × amplitude. Volatilité récompensée. |
| **D10** | Hot-lists par marché | 🛠 implémenté | Une hot-list par venue (TW/EU/US). Univers actif = `sticky_all ∪ union(marchés ouverts)`. Classement intra-venue. Sticky hors quota, hors logique ouverture. *(La `sleeve_24/5` figurait au design D10 mais n'est pas implémentée — forex/commodity retirés, pool 100 % actions.)* |
| **D11** | Stops adaptatifs : intention paramétrique résolue au tir | ✅+🛠 Ph1-2 | Late-binding : `hard_stop` armé exprimé en `volatility_multiple`/`percent`/`structural` (ancres chartistes), résolu en prix absolu au **déclenchement** sur vol fraîche. Unifié avec les entrées directes. |
| **D12** | Préflight LLM des ouvertures swing planifiées | 💬 en discussion | Proposition conditionnelle : cross-session / gap d'ouverture / plan âgé / `requires_preflight` → relire la thèse avant RiskGate. Ne modifie pas D7B tant qu'Erwan ne l'a pas validée. |

---

## 4. Frais & break-even

Modèle de frais IBKR simulé (`execution/broker.py`, `CommissionModel`). Le break-even
en bps (`be_ref_bps`) et le coût aller-retour (`rtrip_bps`) pour un ordre de référence
= `max_order_value` sont injectés dans le cockpit — l'agent peut comparer l'amplitude
attendue au coût avant de scalper.

⚠️ P&L paper < 2026-06-13 = **brut** (frais = 0). La conscience-frais a été livrée
le 2026-06-13 (commit `390a3ea`). Les statistiques mélangeant avant/après peuvent
donner un biais favorable.

Les P&L dans `model_performance.jsonl` sont en **devise locale** (BP.L = pences, CHF,
USD…). `attribution.py` additionne sans conversion → à reconvertir avant de comparer.

---

## 5. Attribution

`trader/attribution.py` — calcule des round-trips (entrée + sortie appariés) et
produit `by_exit_reason`, `by_confidence_bin`, coût moyen par trade.

Injecté dans `base_context["attribution"]` à chaque cycle : l'agent voit sa propre
performance et calibre ses décisions.

`attribution_since` (configurable dans `regime.yaml`) filtre les trades : la valeur
courante est **`2026-06-10`** — les trades clôturés **avant le 10/06 sont exclus** des
statistiques d'attribution vues par l'agent. Cette frontière correspond à la mise en
place du gate confiance×risque (commit `dffb6d1`). Les KPI du cockpit et les
round-trips ne reflètent donc **pas** les pertes antérieures (CL=F 09/06, exit_plans
invalides 05-08/06).

**Tagging des sorties LLM** : avant commit `17bfb62` (2026-06-15), les sorties
`CLOSE` n'avaient pas de tag `exit_reason` → elles apparaissent comme `"close"`
(intent brut) dans les stats. Les occurrences de `"close"` + `"llm_exit"` sont
les **mêmes** sorties LLM (artefact temporel, pas deux chemins concurrents).
Voir `docs/analysis/comportement-sorties.md` §2.

---

## 6. Consolidation des learnings

`trader/consolidator.py` — quand `learnings.jsonl` dépasse
`DEFAULT_CONSOLIDATION_THRESHOLD` entrées brutes, un appel au consolidateur (acpx,
modèle séparé `glm-5.1:cloud`) produit `learnings_consolidated.json`.

Prompt de consolidation (D6 étape 1) :
- Cap ≤ 5 règles d'abstention dans le global consolidé
- Priorité aux patterns d'entrée sur les règles d'interdiction
- Seuil de robustesse : ≥ 3 occurrences

`guardrails.json` (invariants humains, immuable) est injecté séparément — guidance
au LLM : « guardrails = invariants, patterns = remettables en question ».

Les bruts ne sont plus réinjectés quand un consolidé existe (évite
l'auto-renforcement des HOLD). `consolidator.py:build_context_learnings`

---

## 7. Post-mortems

| Fichier | Statut | Résumé |
|---|---|---|
| `docs/postmortems/2026-06-08-exit-plans-invalides.md` | ✅ Résolu | 7 rejets `exit_plan` invalides (05-08/06) : contrat exit_plan non exposé au LLM → corrigé `a3357d5`. Zéro récidive. |
| `docs/postmortems/2026-06-09-short-clf-hard-stop.md` | ✅ Résolu | CL=F short confiance 0.58, stop 1R, −101 $ : trade pris à l'extrême baissier. A conduit au gate confiance×risque (`dffb6d1`). |
| `docs/postmortems/2026-06-15-stops-figes-armes-open-eu.md` | ✅ Résolu | CFR.SW + ASML.AS (−95 €) : stops absolu figés à l'armement pré-open EU, trop serrés au tir (~0.84×/1.26× vol). Corrigé par D11 (late-binding). |

---

## 8. Points ouverts connus (backlog post-MVP)

- **Unification direct/armé complète** : les entrées directes `OPEN_LONG`/`OPEN_SHORT`
  passent désormais par `resolve_exit_plan`, mais un stop absolu `type:"price"` trop
  serré fourni directement par le LLM n'est pas recalibré. Recommandation : forcer
  le relatif en direct.
- **REVERSE sans stop** : `REVERSE` n'est pas bloqué par `missing_hard_stop`
  (`daemon.py:1934`) — **à corriger** (backlog, pas un choix intentionnel confirmé
  par analyse Codex).
- **`last_llm_at` volatile** : réinitialisé au restart → le gate de revue périodique
  déclenche une revue globale après redémarrage (délibéré, mais consomme du budget).
- **Debounce gate régime** : un régime fort persistant re-déclenche ~30 min — reliquat
  de coût non éliminé.
- **Rationale de sortie LLM** : `Decision.rationale` est dans `decisions.jsonl` mais
  pas dans `model_performance.jsonl` → invisible pour l'attribution. Propagation
  simple à faire (`exit_rationale`). Voir `docs/analysis/comportement-sorties.md` §5.
- **D3** (réveil cluster univers), **D4** (RVOL/ATR cockpit), **D6 étapes 3-4**
  (missed_moves contrafactuels, by_symbol scopé) : validés, non implémentés.
- **DST / fériés / demi-séances** : heures de session UTC codées manuellement dans
  `config/sessions.yaml`. À terme : `timezone` + `valid_from/to` + jours fermés.

---

*Doutes / points à valider humainement listés dans le résumé de livraison.*
