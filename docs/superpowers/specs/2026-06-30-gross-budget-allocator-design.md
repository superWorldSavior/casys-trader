# Admission gross équitable par ordonnancement (zéro clamp)

**Date** : 2026-06-30
**Statut** : ✅ **IMPLÉMENTÉ** (TDD) — `trader/gross_priority.py` +
réordonnancement dans `trader/daemon.py` (boucle d'exécution du cycle).

> **Note de pivot** : une 1ʳᵉ version envisageait un *allocateur* qui clampe les
> qtys (parts pro-rata / partielles). Abandonnée : le pattern maison est
> **advisory + rejet** (jamais de down-size silencieux ; cf `risk.py` qui rejette
> et ne clampe pas), et la consigne produit est **laisser la liberté de sizing à
> l'agent**. La solution retenue ne clampe RIEN.

---

## 1. Contexte & problème

Plafond d'exposition brute `max_gross_exposure = 100000` (`config/risk.yaml:6`).
Le `RiskGate` (`trader/risk.py:255-257`) **rejette** un ordre si
`projected_gross > max_gross_exposure`. Il n'y a pas de clamp d'entrée
(`_clamp_exit_quantity` ne touche que les sorties).

Le défaut n'est PAS le rejet (c'est le fusible voulu) — c'est **qui** se fait
rejeter. La boucle d'exécution (`daemon.py`, `for … in symbols_to_decide`) traite
les décisions dans l'ordre de la **liste**, et `gross` est une variable
**courante** mise à jour après chaque fill. Donc le premier symbole de la liste
qui ouvre rafle la marge ; les suivants tapent le plafond et sont rejetés —
**arbitraire** (ordre de liste), pas au mérite. Observé 29/06 : 5 rejets
`risk:gross_exposure_exceeded` sur book ~plein (99.7k/100k).

Cause amont de l'over-sizing (hors périmètre ici, cf §6) : `_max_quantity_for_symbol`
calcule `gross_remaining` **une fois par cycle**, mais le batch lance jusqu'à 3
appels concurrents (`decision_batch_parallelism=3`) qui reçoivent **le même
snapshot** → chacun se size comme s'il avait tout le budget.

## 2. Objectif

Rendre l'arbitrage **déterministe et au mérite**, **sans rien clamper** : un ordre
passe à **pleine taille choisie par l'agent**, ou est rejeté proprement. Quand la
marge gross est épuisée, ce sont les ouvertures **les plus basse-conviction** qui
sautent — pas les dernières de la liste.

## 3. Décisions (validées)

- **Zéro clamp.** On ne réduit jamais une qty (liberté de sizing à l'agent).
  L'ordre-frontière qui ne rentrerait que partiellement est **rejeté proprement**,
  pas rogné.
- **Mérite par conviction.** Les ouvertures sont servies par `confidence`
  décroissante.
- **Sorties d'abord.** Les réducteurs (CLOSE/REDUCE) s'exécutent avant les
  ouvertures → ils libèrent de la marge que le gate accorde naturellement aux
  ouvertures suivantes (le `gross` courant baisse avant qu'elles ne soient
  évaluées). Pas de calcul d'allocation : c'est l'ordre d'exécution + le gate.
- **Déterminisme.** Tie-break `symbol` croissant. Aucune dépendance temps/hasard.
- **Plans armés : même pool.** Ordonnés par leur `confidence` stockée, comme les
  entrées fraîches.

## 4. Architecture

### 4.1 Primitive pure — `trader/gross_priority.py`

```
gross_execution_order(items: list[PriorityItem]) -> list[str]
PriorityItem(symbol, intent, confidence)
```

Tri par clé `(phase, −confidence si ouverture sinon 0.0, symbol)` :
- phase 0 : réducteurs `CLOSE`/`REDUCE`
- phase 1 : ouvertures `OPEN_LONG`/`OPEN_SHORT`/`REVERSE`, par conviction ↓
- phase 2 : le reste (`HOLD`, etc. — ne consomme pas de gross)

Pure, déterministe, testable isolément. Ne connaît ni prix, ni gross, ni gate.

### 4.2 Intégration daemon

Juste après l'assemblage final de `decisions_by_symbol` (plans armés mergés) et
**avant** la boucle d'exécution : `symbols_to_decide` est réordonné via
`gross_execution_order`. La boucle et le `RiskGate` sont **inchangés** — c'est le
nouvel ordre qui rend leur rejet méritocratique. Aucune qty modifiée.

### 4.3 Conséquence

- Book plein + une ouverture haute conviction + une basse : la haute passe (pleine
  taille), la basse est rejetée `risk:gross_exposure_exceeded` (au lieu de l'inverse
  selon l'ordre de liste).
- Sortie + ouverture le même cycle : la sortie s'exécute d'abord, libère la marge,
  l'ouverture la récupère via le gate (running `gross` à jour). « Sorties d'abord »
  obtenu par l'ordre d'exécution, sans allocateur.

## 5. Cas limites & risques

- **`max_orders_per_cycle` (=5)** : les réducteurs consomment désormais les slots
  d'ordres **avant** les ouvertures. Effet voulu (de-risk d'abord) mais c'est un
  changement de comportement à noter : un cycle saturé de sorties peut bloquer des
  ouvertures par `order_rate_exceeded`. Acceptable (fusible débit).
- **`REVERSE`** : classé en ouverture (phase 1), ordonné par conviction. Cohérent
  avec son traitement existant (« tracé mais pas clampé », dette connue).
- **`dry_run`** : `gross` n'est PAS recalculé entre fills simulés (le refresh est
  sous `if not dry_run`). L'ordre d'exécution reste correct, mais l'admission
  simulée diverge du réel : elle peut **sur-admettre** des ouvertures entre elles
  (toutes voient le `gross` de début) **et sous-admettre** après une sortie simulée
  (la marge libérée n'est pas reflétée). Artefact de simulation, sans effet réel.
- **Confiance non finie** (NaN/inf) : possible en paper (gate confiance off) →
  normalisée à la conviction la plus basse dans la clé de tri, pour garder le
  déterminisme (sinon les comparaisons NaN, toujours fausses, rendraient l'ordre
  dépendant de l'entrée). Testé.
- **Reproductibilité** : `gross_execution_order` est invariant par permutation de
  l'entrée, y compris avec des confidences non finies (testé).

## 6. Hors périmètre (YAGNI)

- **Clamp / parts partielles** : explicitement écarté (cf pivot).
- **Advisory honnête sous parallélisme** : réserver une tranche de budget par appel
  concurrent du batch pour que `gross_remaining` poussé à l'agent soit exact — la
  vraie correction de la cause amont. Plus gros, séparé.
- **Feedback structuré du rejet dans le prompt** : le rejet est loggé
  (`decisions`/`events`) ; sa ré-injection au cycle suivant est un chantier à part.
- Conversion FX (`casys-trader-chantier-fx-conversion`).

## 7. Tests (TDD)

Primitive `gross_execution_order` (`tests/test_gross_priority.py`) :
1. réducteurs avant ouvertures ;
2. ouvertures par conviction décroissante ;
3. le reste (HOLD) après les ouvertures ;
4. tie-break symbole déterministe ;
5. ordre complet trois phases ;
6. invariance par permutation de l'entrée (déterminisme) ;
7. confiance non finie (NaN/inf) → traitée comme basse conviction, déterministe.

Intégration : couverte par la suite daemon existante (non-régression, 1620 passed)
+ revue Codex. Un test end-to-end « admission par conviction sur book plein »
reste un renfort possible (harness daemon lourd).
