# Allocateur de marge gross déterministe (greedy-conviction)

**Date** : 2026-06-30
**Statut** : 📐 **DESIGN VALIDÉ** — à implémenter (TDD).

---

## 1. Contexte & problème

Le portefeuille a un plafond d'exposition brute `max_gross_exposure = 100000`
(`config/risk.yaml:6`). Deux mécanismes touchent ce plafond :

- **Advisory** : `_max_quantity_for_symbol` (`trader/daemon.py:444`) calcule
  `max_buy_qty`/`gross_remaining_usd` par symbole et les **pousse à l'agent** dans
  son contexte (`codex_client.py:251-256`). L'agent est *censé* se sizer dedans.
- **Gate dur** : `RiskGate` (`trader/risk.py:255-257`) calcule
  `projected_gross = gross − |position courante| + |position projetée|` et
  **rejette en bloc** (`Verdict(False, "gross_exposure_exceeded")`) si
  `projected_gross > max_gross_exposure`.

**Aucun clamp automatique des entrées.** `_clamp_exit_quantity`
(`trader/daemon.py:632`) ne clampe que les sorties (`CLOSE`/`REDUCE`, ligne 639).
Les entrées `OPEN_LONG`/`OPEN_SHORT` qui dépassent la marge ne sont pas réduites :
elles tapent le gate et sont **rejetées entières**.

Symptôme observé (29/06) : 5 rejets `risk:gross_exposure_exceeded` (3 LLM, 2 plans
armés), tous des ordres qui augmentent le gross, book ~plein (dernier fill à gross
99.7k/100k). Deux défauts :

1. **Rejet en bloc** au lieu de réduire à ce qui rentre — un trade qui aurait pu
   entrer en plus petit est perdu.
2. **Non déterministe / injuste** : sous `decision_batch_parallelism=3`
   (`daemon.py:135`), les appels concurrents reçoivent le **même snapshot** de
   marge et se sizent chacun comme si toute la marge était libre ; à l'exécution
   ils dépassent collectivement et le gate en recale **selon l'ordre d'arrivée des
   threads** (arbitraire, irreproductible).

## 2. Objectif

Remplacer le rejet arbitraire par une **allocation déterministe au mérite** de la
marge gross résiduelle, côté daemon, avant le RiskGate. « Juste » ici =
**reproductible et fondé sur la conviction** : financer à fond les meilleures
convictions dans un ordre stable, clamper l'ordre-frontière, skipper le reste —
plutôt que saupoudrer ou laisser le hasard décider. (Une demi-position avec stop
casse le R:R ; mieux vaut une position pleine sur la meilleure idée.)

## 3. Décisions (validées)

- **Politique d'allocation** : **greedy par conviction**. Tri `confidence`
  décroissante ; on remplit chaque ordre à fond jusqu'à épuiser la marge ;
  l'ordre-frontière obtient une part **partielle** ; les suivants **zéro**.
- **Plans armés** : **même pool**, triés par leur `confidence` stockée à
  l'armement. Traitement uniforme avec les entrées fraîches (pas de priorité).
- **Sorties d'abord** : les réducteurs (`Δ ≤ 0`) passent tels quels et **libèrent
  du gross** ; le headroom alloué aux entrées est calculé **après** les avoir pris
  en compte.
- **Déterminisme** : clé de tri `(−confidence, symbol)`. Aucune dépendance au
  temps/hasard. Mêmes entrées → même allocation (AX principe 6).
- **Garde dust** : une qty allouée sous le minimum tradable → qty 0, marqué
  `skipped` (pas d'ordre poussière).

## 4. Architecture

### 4.1 Nouvelle primitive pure

```
allocate_gross_budget(
    orders: list[ProposedOrder],   # symbol, intent, action, qty, confidence,
                                   # price, fx_rate, current_position_value_usd
    gross_exposure: float,
    max_gross_exposure: float,
    min_order_notional_usd: float = 100.0,   # nouveau, optionnel dans risk.yaml
) -> list[AllocationResult]        # order clampé + outcome
```

`AllocationResult` : `{order (qty clampée), outcome: "full"|"partial"|"skipped"|"reducer", allocated_usd}`.

Pure, déterministe, sans I/O. Vit dans un module dédié (ex. `trader/gross_budget.py`)
ou à côté du sizing existant — à trancher au plan selon les imports.

### 4.2 Algorithme

1. **Delta gross par ordre** : `Δ = |position projetée| − |position courante|`
   (en USD), même base que `risk.py:255`.
2. **Réducteurs** (`Δ ≤ 0`) : `outcome="reducer"`, qty inchangée. Leur `Δ` (négatif)
   est sommé pour agrandir le headroom.
3. **Headroom** = `max_gross_exposure − (gross_exposure + Σ Δ_réducteurs)`.
4. **Augmenteurs** (`Δ > 0`) triés `(−confidence, symbol)`.
5. **Greedy** : pour chaque, `Δ_alloué = min(Δ_demandé, max(0, headroom))` ;
   `qty_allouée = qty_demandée × (Δ_alloué / Δ_demandé)` ; `headroom −= Δ_alloué`.
   - `Δ_alloué == Δ_demandé` → `full`
   - `0 < Δ_alloué < Δ_demandé` → `partial`
   - `Δ_alloué == 0` → `skipped` (qty 0)
6. **Garde dust** : si `qty_allouée × prix_usd < min_order_notional_usd` →
   `skipped`, qty 0, et le `Δ` est **rendu au headroom** (non consommé) pour
   l'ordre suivant. Défaut `min_order_notional_usd = 100` ; nouveau paramètre
   optionnel dans `risk.yaml` (le réglage fin lié à la commission, notamment le
   plancher TW, relève du chantier FX).

### 4.3 Intégration daemon

Appelé après l'assemblage des décisions du batch, **avant** le RiskGate, sur
l'ensemble entrées fraîches + plans armés déclenchés du cycle. La qty clampée
remplace la qty proposée et arrive au gate (qui ne trippe plus sur le gross — le
check devient une assertion de sûreté). Caps par ordre / par position : inchangés,
le gate les gère.

### 4.4 Observabilité

Issue enregistrée dans la décision/event : `gross_budget:full|partial|skipped`
(+ `allocated_usd`). Permet de voir au cockpit/journal pourquoi une qty a été
réduite ou un symbole skippé. (La ré-injection de cette issue dans le **prompt**
du cycle suivant — feedback à l'agent — est **hors périmètre** ici.)

## 5. Cas limites

- **Book plein** (headroom ≤ 0) : tous les augmenteurs `skipped` ; les réducteurs
  passent (et peuvent rouvrir du headroom non utilisé ce cycle).
- **Égalité de conviction** : tie-break `symbol` croissant → déterministe.
- **`REVERSE`** (réduit la position courante puis ouvre dans l'autre sens) :
  classé par son **delta net**. S'il augmente le gross net, il entre dans le pool
  greedy et le clamp réduit la **jambe d'ouverture** ; sinon réducteur. Détail
  d'implémentation (calcul de `projected_position` signé) à verrouiller au plan.
- **Prix / fx manquant ou non fini** : ordre exclu de l'allocation (qty 0,
  `skipped`), cohérent avec le garde-fou existant de `_max_quantity_for_symbol`.

## 6. Hors périmètre (YAGNI)

- Caps `max_order_value` / `max_position_value` (déjà gérés par le gate).
- La conversion FX base-USD (chantier séparé `casys-trader-chantier-fx-conversion`).
- La ré-injection du verdict/issue dans le prompt de l'agent (feedback loop).
- Toute modification de `decision_batch_parallelism` (le réglage reste à 3 ;
  l'allocateur rend la concurrence inoffensive vis-à-vis du gross).

## 7. Tests (TDD, invariants d'abord)

1. 1 augmenteur qui rentre sous le headroom → inchangé (`full`).
2. 2 augmenteurs, total > headroom → +convaincu `full`, l'autre `partial`
   (qty = part restante).
3. 3 augmenteurs, headroom épuisé → le 3ᵉ `skipped` (qty 0).
4. Réducteur dans le même cycle → libère du gross → un augmenteur qui ne rentrait
   pas rentre.
5. Égalité `confidence` → ordre `symbol` déterministe (mêmes entrées, même sortie).
6. Allocation partielle sous `min_order_notional_usd` → `skipped`, pas d'ordre dust.
7. Book plein (headroom ≤ 0) → tous augmenteurs `skipped`, réducteurs passent.
8. Plan armé (qty figée + confidence stockée) traité comme un augmenteur du pool,
   clampé/skippé selon sa conviction.
9. Prix/fx non fini → `skipped`.
10. Déterminisme global : permuter l'ordre d'entrée de la liste ne change pas
    l'allocation.
