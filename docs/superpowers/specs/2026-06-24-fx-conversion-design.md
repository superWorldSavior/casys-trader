# Conversion FX — comptabilité et sizing en devise de base (USD)

**Date** : 2026-06-24
**Statut** : design validé, à implémenter
**Décision métier associée** : D14 (à ajouter au registre)
**Incident déclencheur** : trade Realtek `2379.TW` du 2026-06-23

---

## 1. Problème

Le système n'a **aucune conversion de devise**. Tous les montants natifs
(prix, cash, P&L, commissions) sont manipulés comme s'ils étaient dans une
devise unique. L'univers courant étant à 92 % taïwanais (24/26 symboles en TWD),
le book est massivement faussé.

### Incident Realtek (preuve)

- BUY 10 @ 870 TWD (commission 80 TWD), SELL 10 @ 829 TWD (commission 80 TWD).
- P&L net affiché : **−570**, en réalité en **TWD** (≈ −16,5 €), pas en base.
- Cause racine du *trade*, pas seulement de l'affichage : la **taille de
  position** est calculée en USD implicite sur des prix natifs. L'agent et le
  risk gate ont cru bâtir un ordre de ~8 700 $ ; c'était ~250 $ (8 700 TWD).
- Position accidentellement minuscule → le plancher de commission fixe IBKR
  Taiwan (80 TWD/ordre) pèse **184 bps aller-retour** → perte structurelle.

### Les points aveugles aux devises (avant fix)

| Couche | Code | Bug |
|---|---|---|
| Sizing par valeur | `risk.py:84` `max_order_value / price` | plafond `$` ÷ prix natif → position ~35× trop petite en TWD |
| Sizing au risque | `risk.py:119` `risk_cap / distance` | `risk_cap`(USD) ÷ distance natif → risque réel ≈ 3 % du budget visé |
| Bornes risque | `risk.yaml:5-10` (`max_position_value`, `max_gross_exposure`, `min_equity`) | toutes en `$`, comparées à des valeurs natives |
| Cash broker | `execution.py:252` `cash -= signed*price + commission` | déduit du natif d'un pool USD |
| P&L par leg | `attribution.py:398` `_position_pnl` | aucune conversion |
| Agrégat / equity | `attribution.py:224` `sum(pnls)` | somme TWD + USD + EUR comme homogènes |
| Affichage | `tui.py` (commit `e7a1b28`) | maquillé en « non conv. »/« local », pas converti |
| Quantité agent | `daemon.py:2061` `decision.quantity` | le LLM propose un nb d'actions en USD implicite |

---

## 2. Décisions de design (validées)

1. **Devise de base = USD.**
2. **Garder l'univers multi-devises** (TW/EU/US). On ne l'ampute PAS : le
   problème Realtek est un problème de *taille*, pas de *devise*. Une fois le
   sizing converti, les frais redeviennent négligeables (~0,025 %) sans veto
   frais ni restriction d'univers.
3. **Source FX = yfinance live, cachée par cycle, fallback statique.** Cohérent
   avec le fail-safe existant et le principe « faits calculés par le code ».
4. **Déterminisme : `fx_rate` persisté sur chaque fill.** L'attribution
   historique reconvertit exactement, sans dépendre du taux courant (AX #6).
5. **L'agent dimensionne en devise native ; le code lui livre son budget
   converti.** L'agent a déjà tout le contexte natif (bougies, chart, niveaux) :
   il place ET dimensionne ses ordres dans la devise de cotation du symbole. Le
   code calcule et injecte par symbole le **budget de risque déjà converti en
   natif** (`risk_budget_native = risk_pct × equity_USD ÷ fx_rate`) et le
   **plafond d'ordre natif** (`max_order_value ÷ fx_rate`). L'agent fait alors
   son sizing en pur natif (`quantity = risk_budget_native ÷ distance_stop`),
   sans jamais convertir. Le risk gate reste le **fusible** : il clampe en USD
   (currency-correct, cf. §4.1), backstop si l'agent sur-dimensionne. Unique
   conversion = côté code (USD→natif pour le budget), jamais côté agent.
6. **Migration : recalcul du cash depuis l'historique des fills** avec FX
   rétroactif, one-shot, backup `broker.json.bak`.
7. **Deux référentiels qui ne se mélangent jamais** :
   - **USD = vue humaine + vérité comptable** (cockpit, cash, equity, P&L, et le
     *budget* de risque, ancré sur l'equity USD). C'est ce qu'Erwan lit.
   - **Natif = vue interne de l'agent** : il analyse, pose ses niveaux ET
     dimensionne par symbole dans la devise de cotation, **sans jamais
     convertir** (le code lui livre son budget déjà converti en natif). L'analyse
     (bougies, indicateurs) n'est JAMAIS convertie — le FX y injecterait du
     bruit qui corrompt les indicateurs. La conversion n'a lieu qu'au pont du
     sizing (code) et à l'affichage cockpit (humain).

---

## 3. Architecture

### 3.1 Module `trader/fx.py` (nouveau)

Unité à responsabilité unique : convertir `(montant, devise) → USD`.

- `currency_for(symbol: str) -> str` : dérive la devise de cotation du suffixe
  (`.TW`/`.TWO` → TWD, `.PA`/`.DE`/`.AS`/`.MI` → EUR, `=X`/`=F`/`^`/défaut →
  USD). Table extensible, source unique de vérité.
- `to_usd(amount: float, ccy: str, rate: float) -> float` : conversion pure,
  `rate` = USD par unité de `ccy` (ex. TWD→USD ≈ 0,031). `ccy == "USD"` → rate 1.0.
- Pas d'I/O dans ce module : il reçoit le `rate`, il ne le fetche pas
  (testable, déterministe, contrat étroit).

### 3.2 Fetch des taux (dans la couche données / daemon)

- À chaque cycle, après le fetch des prix, récupérer les paires FX nécessaires
  pour les devises présentes dans l'univers (`TWD=X`, `EURUSD=X`, …).
- Stocker la table `{ccy: rate_usd}` dans le snapshot du cycle (à côté des prix).
- Fallback : table statique en config (`config/fx.yaml`) si yfinance échoue,
  loggé comme dégradation (cohérent avec le retry IB lazy).
- Le **même taux du cycle** sert au sizing ET au fill papier de ce cycle.

### 3.3 Persistance sur le fill

`Fill` gagne `fx_rate: float` et réutilise `commission_currency` déjà présent.
Stocké à l'exécution. L'attribution lit `fx_rate` du fill (jamais le taux du
jour) → reproductibilité.

---

## 4. Les frontières corrigées

### 4.1 Sizing — `trader/risk.py`

- `max_order_quantity_at_price` : convertir le plafond OU le prix dans la même
  devise avant division. Reçoit un `rate` (ou un prix déjà-USD) en paramètre.
- `max_quantity_at_risk` : `distance` convertie en USD avant
  `quantity = risk_cap / distance_usd`. `equity` est déjà USD (cf. §4.3).
- Bornes `max_position_value`, `max_gross_exposure`, `min_equity` : comparées à
  des valeurs **converties USD**.
- **Voie principale de sizing = l'agent en natif** (cf. §4.5/§4.6) : le code lui
  fournit `risk_budget_native` et `max_order_native`, l'agent propose la
  quantité. Le risk gate reste le **fusible** : il clampe la proposition de
  l'agent contre les bornes (en USD, conversion côté gate). Il ne remplace pas
  le jugement de l'agent, il le borne.

### 4.2 Cash — `trader/tools/execution.py`

- `IbkrCommissionModel.calculate` : inchangé (retourne déjà la devise native +
  `currency`). La commission reste native sur le fill ; conversion à la lecture.
- `PaperBroker._fill` : `self._state.cash -= to_usd(signed*price, ccy, rate) +
  to_usd(commission.amount, commission.currency, rate)`. Stocker `fx_rate` sur
  le fill.
- `cash`/`equity` deviennent strictement USD.

### 4.3 P&L — `trader/attribution.py`

- `_position_pnl` et `compute_round_trips` : convertir chaque leg via le
  `fx_rate` du fill (entrée ET sortie peuvent avoir des taux différents — gérer
  les deux). `gross_pnl`, `commission`, `pnl` en USD.
- `_aggregate` : `sum(pnls)` devient une somme homogène en USD (correct sans
  changement de code une fois les trips convertis).

### 4.4 Affichage — `trader/tui.py` / `trader/cockpit.py`

- Tous les montants en USD ; retirer les labels « non conv. »/« local » et
  réafficher le symbole `$`.
- Les **niveaux de prix par titre restent natifs** (un stop Taiwan reste à
  829 TWD — un prix converti n'a pas de sens technique). Afficher la devise
  native à côté des prix de niveau, l'USD pour tout ce qui est portefeuille.

### 4.5 Contrat agent — `trader/codex_client.py`

- L'agent **continue de proposer `quantity`**, mais en **devise native** du
  symbole, dimensionnée avec le `risk_budget_native` que le code lui fournit
  (cf. §4.6). `quantity` reste dans `_DECISION_KEYS`.
- Le prompt explique le sizing natif : « Dimensionne en unités du titre.
  `risk_budget_native` = ce que tu peux risquer dans la devise du symbole ;
  `quantity ≈ risk_budget_native ÷ distance_au_hard_stop`, borné par
  `max_order_native`. Ne convertis jamais : tout est déjà dans la bonne devise. »
- `daemon.py:2061` : `effective_quantity = abs(decision.quantity)` (natif),
  puis clampé par le risk gate (fusible currency-correct, §4.1).

---

### 4.6 Conscience devise du contexte agent — `trader/agent_context.py`

L'analyse reste en natif, mais le natif doit être **étiqueté** pour que l'agent
sache toujours dans quel référentiel il agit (sinon il lit `p: 829` sans savoir
que c'est du TWD).

- `build_market_cockpit` : chaque ligne symbole gagne `ccy` (devise de cotation,
  via `fx.currency_for`), `fx_usd` (taux du cycle, pour l'ordre de grandeur),
  `risk_budget_native` (= `risk_pct × equity_USD ÷ fx_usd`, ce que l'agent peut
  risquer dans la devise du symbole) et `max_order_native`
  (= `max_order_value ÷ fx_usd`). Exemple : `{"s": "2379.TW", "ccy": "TWD",
  "p": 829, "fx_usd": 0.031, "risk_budget_native": 32258, "max_order_native":
  322580, ...}`.
- **Aucune valeur d'analyse n'est convertie ici** : `p`, indicateurs, swings
  restent natifs. `fx_usd` est informatif ; `risk_budget_native`/
  `max_order_native` sont les seuls montants, déjà exprimés en natif par le code
  pour que l'agent dimensionne sans convertir.
- Prompt (`codex_client.py`) : règle explicite — « Tous les prix, indicateurs,
  swings et niveaux d'un symbole sont dans sa devise `ccy`. Tes `hard_stop`,
  `take_profits` ET ta `quantity` sont dans cette MÊME devise. Dimensionne avec
  `risk_budget_native` (quantity ≈ risk_budget_native ÷ distance_hard_stop),
  borné par `max_order_native`. Tu ne convertis rien. »
- Le bloc portefeuille du contexte (equity, cash, exposition) est, lui,
  explicitement en USD (vue d'ensemble ; le sizing par symbole se fait en natif).

## 5. Migration de l'état existant

Script one-shot (`scripts/`), `dry_run` par défaut (AX #2) :

1. Backup `state/broker.json` → `state/broker.json.bak-pre-fx-YYYYMMDD`.
2. Rejouer `fills` dans l'ordre : pour chaque fill sans `fx_rate`, récupérer le
   taux historique à `ts` (yfinance close de la paire FX ce jour-là), le
   stocker sur le fill, recalculer le cash en USD.
3. Recalculer `cash` final = `starting_cash − Σ to_usd(...)`.
4. Vérifier la cohérence (positions ouvertes valorisées, equity plausible) et
   logger l'écart avant/après (mesure de l'ampleur du bug).
5. Écriture seulement si `--commit`.

---

## 6. Invariants / tests (test-first, AX #11)

- `to_usd(x, "USD", r) == x` pour tout `r`.
- `to_usd(amount, ccy, rate)` linéaire ; `rate <= 0` ou non-fini → rejet explicite.
- `currency_for` couvre tous les suffixes de l'univers courant + défaut USD.
- Contexte agent : `risk_budget_native = risk_pct × equity_USD ÷ fx_usd` et
  `max_order_native = max_order_value ÷ fx_usd` (un titre TWD à rate 0,031,
  equity 100 000 $, risk_pct 1 % → `risk_budget_native ≈ 32 258 TWD`).
- Sizing agent en natif : `quantity ≈ risk_budget_native ÷ distance_hard_stop` ;
  l'exposition USD résultante ≈ `risk_pct × equity` (pas ×FX).
- Risk gate (fusible) : un titre TWD à 870 (rate 0,031), plafond ordre 10 000 $ →
  clampe à ~370 actions max (pas 11) ; `quantity * price * rate ≤ max_order_value`.
- Cash : BUY puis SELL d'un titre TWD ne déduit que les frais convertis USD.
- Attribution : un round-trip TWD donne le P&L en USD = (P&L natif) × rate ;
  agrégat USD homogène avec un mix TWD/USD.
- Déterminisme : recalcul de l'attribution sur les mêmes fills (avec `fx_rate`
  figé) → résultat identique quel que soit le taux courant.
- Migration : `dry_run` ne mute rien ; `--commit` produit un cash USD cohérent
  et un backup.

---

## 7. Hors scope (YAGNI)

- Veto frais / garde-fou min-notionnel : **inutile** une fois le sizing correct.
- Restriction de l'univers à l'USD : rejetée (cf. §2.2).
- Couverture de change, exposition FX comme classe d'actif : non.
- Multi-base (EUR) : base = USD point. Extensible plus tard si besoin.
