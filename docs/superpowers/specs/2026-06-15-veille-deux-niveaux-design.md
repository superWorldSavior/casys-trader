# Veille à deux niveaux — radar large + rotation du hot-set

**Date** : 2026-06-15
**Statut** : design validé (révisé post-review Codex), prêt pour plan d'implémentation
**Auteurs** : Erwan + Claude (brainstorming), review indépendante Codex (3 axes)

## 0. Révisions (post-review Codex, 2026-06-15)

Review fan-out 3 axes (stratégie / edge cases / faisabilité), 28 findings → 14 retenus.
Amendements intégrés ci-dessous :
- **Score** : la volatilité n'est plus un malus. On veut *tendance propre ET amplitude*
  (efficacité de tendance × amplitude récompensée bornée + plancher d'éligibilité).
- **Contrat d'horizon** explicité : radar = éligibilité + biais ; timing au hot-path 15m.
- **Concentration** : assumée (pas de garde-fou à la sélection) ; réserve sizing corrélé.
- **2 bloquants** : écriture atomique de `universe.yaml` ; `sticky` calculé avant écriture.
- **3 corrections factuelles** : `starting_cash` (`daemon.py:1102` + `stats.py:114`) ;
  `NG=F` absent de `regime.yaml` ; contrefactuel non branchable sur `decision-bench`.
- **Mesure d'alpha** : harness `rotation_ledger` + bench de rotation as-of, codé *en
  parallèle* de l'implémentation (pas de gate préalable).

## 1. Problème

Aujourd'hui l'univers est une **liste unique curée à la main** (`config/universe.yaml`,
~13 symboles) et **tout** passe au hot-path 15m (daemon → cockpit → Codex). Monitorer tous
les symboles à court terme coûte cher ; on ne peut pas élargir la veille sans exploser le
coût LLM.

On veut que **le marché choisisse ce qu'on trade** : si un univers surperforme, l'agent a
une veille fine dessus ; s'il décroche, on tourne vers autre chose. Sans payer la veille fine
sur tout, tout le temps.

## 2. Principe : deux niveaux

```
POOL (grand univers liquide borné, ~500-800 symboles, config/pool.yaml)
   │  RADAR daily EOD — 100 % code, 0 LLM, ~gratuit
   │  score composite (éligibilité + biais) → classement déterministe
   ▼
HOT-SET DÉFAUT (≤ M=25)  ← hystérésis (anti-ping-pong) + sortie d'urgence
   │  1 appel Codex / jour : reçoit le radar COMPLET, propose des overrides tracés
   ▼
HOT-SET FINAL  ∪ sticky  ──écrit atomiquement──►  config/universe.yaml (généré)
   │
   ▼
HOT-PATH 15m EXISTANT (daemon + relevance_gate + cockpit + Codex + risk gate)
   inchangé — mais opère sur un set dynamique
```

- **Tier 1 (radar)** : large, lent, bête, pas cher. Sortie = un *classement*, pas des trades.
- **Tier 2 (hot-path)** : le trader actuel, inchangé ; seul le *set de symboles* devient dynamique.

## 3. Décisions verrouillées

| # | Décision | Choix |
|---|---|---|
| 1 | Décideur de la composition du hot-set | **Code défaut déterministe + agent override tracé** |
| 2 | Source de vérité du pool | `config/pool.yaml` (grand univers liquide borné, ~500-800, éditable) |
| 3 | `config/universe.yaml` | **Output généré** (hot-set + sticky), écrit atomiquement, lu par le daemon |
| 4 | Budget du hot-set | **Cap symboles pur**, `M = 25` (slots libres ; sticky hors quota) |
| 5 | Score de classement | **Composite** : efficacité de tendance × force relative × amplitude (bornée), × tilt |
| 6 | Cadence | **Radar daily EOD + hystérésis** (Δ, dwell K) **+ sortie d'urgence** |
| 7 | Convictions | **Tilt de famille optionnel** (`config/conviction.yaml`, vide par défaut, borné/validé) |
| 8 | Exclusions dures (data) | **`pool.yaml` source explicite** (PAS `regime.yaml`), jamais promues |
| 9 | Concentration | **Assumée** (pas de garde-fou à la sélection ; réserve sizing corrélé) |
| 10 | Mesure d'alpha | **Harness as-of en parallèle de l'impl** (rotation_ledger + bench rotation) |

## 4. Contrat d'horizon (radar vs hot-path)

Le radar score sur horizon long (daily, 1-4 semaines) ; le hot-path trade en 15m. **Le radar
ne donne pas un ordre d'entrée.** Il fournit deux choses :
- **éligibilité** : « cet univers est en tendance propre ET offre du mouvement → mets-y ta
  veille fine » ;
- **biais directionnel** (le signe du score) : indication, pas obligation.

Le **timing d'entrée** (continuation vs pullback vs attendre) reste **au hot-path 15m**
(le LLM + les signaux intraday existants). Le radar dit « surveille ça de près », pas
« achète maintenant ». Ça évite d'entrer en veille fine au sommet local d'un leader 4 semaines.

## 5. Score composite (Tier 1)

L'intention : sélectionner des univers qui **tendent proprement** (fiables) **et bougent assez**
(tradables en 15m net de frais). La volatilité n'est **pas** pénalisée — elle est requise.

**Éligibilité** (filtre dur, avant scoring) :
```
éligible(sym) SI  sym ∉ exclusions_dures(pool.yaml)
              ET  data_daily_fraîche(sym)
              ET  ATR%(sym) ≥ atr_floor          # trop calme ne paie pas le spread
```

**Score** (composants ; forme exacte additive/multiplicative + gestion du signe long/short =
point de calibration du plan) :
```
score(sym) = efficacité_tendance_signée(sym)        # propreté : déplacement net ÷ chemin
                                                     #   parcouru (efficiency ratio / ADX), signée
           × force_relative(sym, benchmark_venue)    # surperf vs le marché de SA venue
           × (1 + bonus_amplitude(sym))              # amplitude RÉCOMPENSÉE mais BORNÉE
                                                     #   (sature au-delà d'un ATR% : anti-chaos)
           × (1 + tilt_famille(sym))                 # conviction optionnelle, bornée
```
- **efficacité de tendance** = la « stabilité dans la tendance » d'Erwan. Sépare le trend
  propre du chop. Sa magnitude classe l'attractivité ; son signe donne le biais.
- **force relative par venue** (PAS SPY global) : US→SPY, Euronext→^FCHI, TWSE→indice local,
  FX/crypto→z-score intra-famille (pas d'indice de référence évident). Règle le double comptage
  momentum/RS et l'incohérence de calendrier/devise.
- **amplitude** : récompense saturante + plancher d'éligibilité. « Je veux de la vol » (Erwan),
  mais l'efficacité de tendance évite le pur bruit.
- **séries ajustées** (splits/dividendes) obligatoires pour le radar — sinon faux leaders
  (le fetch yfinance existant est `auto_adjust=False`).

**Exclusions dures** (data, pas opinion) : `pool.yaml` est la **source explicite**
(`CL=F`, `NG=F`, `GC=F` et tout futures à data différée). ⚠️ NE PAS réutiliser `regime.yaml`
(qui ne liste que `CL=F`, `GC=F` pour l'attribution — `NG=F` y est absent). Test dédié.

## 6. Sélection : code défaut + override agent tracé (décision 1)

Pourquoi pas « l'agent décide tout » : la rotation **est** la nouvelle stratégie ; un LLM
non-déterministe dans la boucle casse la backtestabilité. On garde un défaut déterministe
mesurable + une couche d'override mesurée.

1. **Défaut (code, déterministe)** : `rotation.py` applique l'hystérésis au classement.
   - **Hystérésis (anti-ping-pong)** : un froid ne remplace un chaud que si
     `score_entrant > score_sortant + Δ` **et** que le sortant est chaud depuis `≥ K jours`.
   - **Sortie d'urgence (court-circuite le dwell K)** : un chaud est dégradé immédiatement si
     son score passe sous un **seuil absolu**, sur **gap adverse**, ou **invalidation daily** —
     sinon l'hystérésis maintient des *stale losers* K jours (cf. choc/gap).
2. **Sticky (hors quota, AVANT l'écriture — bloquant)** : `sticky_symbols()` central =
   **union** des positions broker ouvertes + plans armés/ouvertures actives + exit watches +
   EXECUTE_ORDER actifs + ordres pending. Ces symboles **ne peuvent jamais être dégradés** et
   sont unionnés au hot-set **avant** d'écrire `universe.yaml`. Raison : `reconcile_universe`
   (`daemon.py:2254`) purge wakes/streaks/watches des symboles absents (`scheduler.py:196/207`)
   et l'exit ignore les plans hors univers (`daemon.py:743`) ; `relevance_gate` ne protège que
   les symboles **déjà** dans l'univers. Sans union préalable, une position hors hot-set perd
   ses gardes.
3. **Cap** : slots libres `= max(0, M − |sticky|)`. Si `|sticky| > M` → slots libres `= 0`,
   alerte `sticky_over_cap` (le hot-path peut dépasser le coût prévu, c'est signalé).
4. **Override (agent, tracé)** : 1 appel Codex/jour. Reçoit le **radar complet** (agrégats par
   famille + détail des ~50 meilleurs prétendants) + le hot-set défaut. Ajoute/retire avec
   **raison loggée** (corrélation, redondance, thèse macro). Override invalide (hors pool,
   dépasse le cap, JSON malformé) → fallback défaut + rejet loggé.

## 7. Composants

| Fichier | Rôle | Ancrage vérifié |
|---|---|---|
| `trader/radar.py` *(nouveau)* | Fonction pure : barres daily ajustées → score composite → classement. Réutilise les primitives de `family_regime` (Bar, familles), **pas** une extension de ce module. | `family_regime.py` |
| `trader/radar_data.py` *(nouveau)* | Couche data radar : `yf.download` **batché** + cache disque + chunks + retry/backoff. PAS le `yf.Ticker().history()` par symbole actuel (`market.py:382`) — fragile à 500-800 symboles (rate-limit 429). | `market.py:382` |
| `trader/rotation.py` *(nouveau)* | Hystérésis + sortie d'urgence → hot-set défaut → `sticky_symbols()` → appel Codex (overrides) → **écriture atomique**. CLI `--run` (pattern `consolidator.py:482`). | `consolidator.py:478` |
| `config/pool.yaml` *(nouveau)* | Pool (~500-800) + **exclusions dures** (source explicite) + filtre liquidité/tradabilité. | — |
| `config/radar.yaml` *(nouveau)* | Params : poids, fenêtres, `atr_floor`, plafond amplitude, benchmarks par venue, `M`, `Δ`, `K`, seuils sortie d'urgence. | — |
| `config/conviction.yaml` *(nouveau)* | Tilt de famille, **vide par défaut**, schéma strict (famille connue, membre éligible, borne numérique, `tilt > -1`). | — |
| `config/portfolio.yaml` *(nouveau)* | `starting_cash` (sorti de `universe.yaml`). Repointer **`daemon.py:1102`** (init `SimBroker`) **et** `stats.py:114`. `cli.py:49` reste sur `universe.yaml` (ne lit que `symbols`). | `daemon.py:1102`, `stats.py:114` |
| `config/universe.yaml` | **Output généré** : `symbols:` écrit atomiquement (tmp + `os.replace`), validé avant replace (`_load_yaml` fait un `safe_load` brut, `daemon.py:195/1097`). | `daemon.py:1089/1097/2253` |
| `state/rotation_ledger.jsonl` *(nouveau)* | Schéma dédié `source="rotation"` : `default_hot_set`, `final_hot_set`, overrides, raisons, `as_of`. Le `decision-bench` par-trade ne mesure PAS un override de composition (`decision_bench.py:194`). | `decision_ledger.py:65` |
| `state/radar_snapshot.json` *(nouveau)* | Provenance machine-readable : scores, raisons, entrées/sorties, data manquante, `as_of` par symbole. | — |

## 8. Cycle quotidien (data flow)

```
EOD (cron, par calendrier de venue : dernière barre daily CLÔTURÉE + grace)
 └─► radar_data.fetch(pool)   : yf.download batché, séries ajustées, cache as-of
 └─► radar.score()            : éligibilité (ATR floor, data fraîche, exclusions) → score
 └─► radar.rank()             : classement déterministe
 └─► rotation.apply()         : hystérésis + sortie d'urgence → hot_set_défaut
 └─► rotation.sticky()        : union positions+plans+watches+pending (hors quota)
 └─► rotation.agent_override(radar_complet, hot_set_défaut) → hot_set_final  [1 appel Codex]
                                échec Codex (timeout/429/crash) → commit du défaut, log override_unavailable
 └─► écriture ATOMIQUE universe.yaml (symbols = final ∪ sticky) + radar_snapshot.json
 └─► rotation_ledger.log(défaut, final, sticky, raisons, as_of)
le daemon 15m enchaîne sur le nouveau set au prochain cycle.
```

## 9. Edge cases & error handling

- **Sticky-hot** → cf §6.2 : union calculée *avant* écriture, hors quota. Couvre exit watches,
  ordres pending, EXECUTE_ORDER actifs — pas seulement « position ou plan ».
- **Écriture concurrente** → tmp + `os.replace` + validation `symbols` ; le daemon garde le
  dernier univers valide, ne réconcilie jamais sur fichier invalide.
- **EOD / calendrier** → « dernière barre daily clôturée + grace » **par venue** (US, Euronext,
  TWSE, FX 24h) ; `as_of` loggé par symbole ; weekends/fériés/demi-séances respectés.
- **Corporate actions** → séries ajustées (split/dividende) pour le radar ; quarantaine des
  fenêtres avec outlier.
- **Lifecycle symbole** → états `active|halted|suspended|renamed|delisted|retired` ; alias/
  tombstone ; alerte manuelle pour un sticky devenu non tradable.
- **Pool mutable** → symbole retiré de `pool.yaml` : plus éligible aux promotions ; non-sticky
  sorti au prochain run ; sticky conservé avec raison `pool_removed_sticky`.
- **Échec radar massif / rate-limit** → seuil minimal de couverture ; sous le seuil, garder le
  dernier hot-set valide (scan partiel = classement biaisé).
- **Data manquante par symbole** → inéligible (fail-safe), loggé.
- **Tilt invalide** → schéma strict, warning/fail-fast machine-readable.
- **Bootstrap (tout premier run, pas de snapshot)** → `rotation --bootstrap` : seed explicite,
  snapshot initial avec raisons, dwell initialisé ; erreur claire si `pool.yaml` manque.
- **Cold start / restart** → seed depuis le dernier `universe.yaml` valide jusqu'à historique
  daily suffisant.
- **No-leader (seuils non franchis)** → garde le hot-set précédent.
- **Hot-set < M** → toléré (moins cher), pas de remplissage forcé.
- **Migration `starting_cash`** → `portfolio.yaml`, repointer `daemon.py:1102` + `stats.py:114`.
- **Bench historique pollué** → `decision_bench` reconstruit avec `_load_universe_symbols()`
  (`cli.py:324/328`) ; avec univers dynamique il faut journaliser `hot_set_final`/
  `universe_symbols` *as-of* dans le ledger/report et reconstruire depuis cet as-of.

## 10. Concentration (décision 9)

Pas de garde-fou de concentration **à la sélection** : la rotation quotidienne + la conviction
+ le multi-venue d'une même thèse (plus de fenêtres horaires = plus de trades) la justifient.
**Réserve actée** : la rotation borne l'exposition dans le *temps*, pas dans la *journée*.
`max_gross_exposure` (`risk.py:30`) borne le levier nominal mais **pas la corrélation** (25
positions d'un même facteur = un pari leveragé sizé comme diversifié). La **diversification
n'est pas un objectif** (drawdown corrélé intra-jour assumé). Option future : sizing
corrélation-aware pour borner le pire jour — hors scope de ce design.

## 11. Mesure d'alpha (décision 10)

Codé **en parallèle** de l'implémentation (pas un gate préalable) :
- `rotation_ledger` : `default_hot_set` vs `final_hot_set` + overrides → mesure l'apport de
  l'agent (contrefactuel de composition).
- **bench de rotation as-of** : hot-set dynamique vs univers statique actuel vs top-liquidité
  vs random stratifié famille. Métriques : turnover, coût LLM, **P&L 15m net conditionné par
  appartenance au hot-set**, drawdown, concentration, par régime (trend/range/choc). La
  rotation déterministe **est** backtestable — c'est tout l'intérêt de la décision 1.

## 12. Ce qui ne change pas

Le hot-path 15m, `relevance_gate`, le risk gate, les watches / plans armés, le cockpit,
l'attribution. On ne touche qu'à *quels symboles* entrent dans le hot-path, pas à *comment*
il trade.

## 13. Tests / invariants (AX test-first)

- `radar.py` (pur) : score déterministe sur barres fixées ; tri stable ; symbole sans data /
  sous ATR floor / exclu → écarté ; séries ajustées.
- `rotation.py` : pas de swap sous `Δ` ni avant `K` jours ; **sortie d'urgence** sur seuil/gap ;
  `sticky_symbols()` = union complète, hors quota, AVANT écriture ; `sticky > M` → slots 0 +
  alerte ; no-leader → inchangé ; cap `M` sur slots libres.
- Écriture : `universe.yaml` via tmp + `os.replace` ; jamais de fichier partiel lu.
- Override agent : raison loggée, défaut préservé ; invalide → fallback + rejet loggé ;
  échec Codex → commit défaut.
- Migration : `starting_cash` depuis `portfolio.yaml` (`daemon.py:1102` + `stats.py:114`) ;
  exclusions dures depuis `pool.yaml` (test `CL=F/NG=F/GC=F`) ; `universe.yaml` généré =
  `symbols:` seul.
- Mesure : `rotation_ledger` distinct du `decision_ledger` par-trade ; bench reconstruit depuis
  l'`as_of` journalisé.

## 14. Paramètres à calibrer (`config/radar.yaml`)

Poids/forme du score (additif vs multiplicatif, gestion signe long/short), fenêtres efficacité
de tendance, fenêtre force relative, `atr_floor` + plafond amplitude, benchmarks par venue,
`M = 25`, `Δ`, `K`, seuils de sortie d'urgence. Calibration via le bench de rotation (§11).

## 15. Hors scope / YAGNI

- Listes-sources dynamiques (constituants d'indices live) : pool statique éditable suffit.
- Sizing corrélation-aware : noté en réserve (§10), pas dans ce design.
- Pas de phasage v1/v2 : tout le mécanisme est dans ce design ; les itérations affinent les
  paramètres, pas l'archi.
