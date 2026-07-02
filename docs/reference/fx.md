# Référence — Conversion FX

> **Type** : Reference (Diátaxis).
> **Code** : `trader/market/fx.py` (pur) + `trader/market/fx_rates.py` (I/O) · **Config** : `config/fx.yaml`
> **Rôle** : tout est valorisé et sizé en **base USD**. L'analyse reste native.

## Principe (invariant)

- **Base = USD.** Sizing, risk gate, exposition, equity : tout en USD.
- **L'analyse reste en devise native** — prix, indicateurs, thèse ne sont JAMAIS
  convertis. Le contexte agent est estampillé de la devise (`ccy`).
- **L'agent ne size pas** en tenant compte du FX : le **code** applique le taux au
  sizing et au fill. L'agent raisonne natif, le code convertit pour les bornes.

> **Pourquoi** : incident Realtek (23/06). Sans conversion, le sizing était aveugle
> aux devises (`max_order_value` USD ÷ prix natif) → positions non-USD ~35× trop
> petites → plancher de commission TW à ~184 bps. Cf. chantier FX.

## `fx.py` — module pur (reçoit le taux, l'applique)

**Convention** : `rate` = **USD par unité de la devise native** (USD → `1.0`).

### `currency_for(symbol) -> str`
Devise de cotation : table exacte `SYMBOL_CCY` d'abord, puis suffixe `SUFFIX_CCY`,
défaut `USD`.

| Résolution | Exemples |
|---|---|
| Exact (`SYMBOL_CCY`) | `^FCHI`→EUR, `^TWII`→TWD |
| Suffixe eurozone | `.PA .DE .AS .MI .BR .HE .LS .MC .VI` → EUR |
| Suffixe autres | `.L`→GBP, `.SW`→CHF, `.CO`→DKK, `.OL`→NOK, `.ST`→SEK |
| Taïwan | `.TW .TWO .T` → TWD |
| Défaut | (aucun suffixe) → USD |

> **Piège documenté** : `.T` = **Taïwan** dans CE pool (variante de `.TW`), **pas
> Tokyo** (confirmé Erwan 24/06 : « y a pas de Tokyo ici »). `endswith` distinct de
> `.TW`/`.TWO`, pas de collision.

### `to_usd(amount, ccy, rate) -> float`
`USD` → identité. Sinon `amount × rate`. **Fail-fast** : lève `ValueError` si le
taux est non-fini ou ≤ 0 (un taux invalide fausserait toutes les bornes).

## `fx_rates.py` — provider de taux (I/O isolée, fetcher injecté)

### `rates_for_symbols(symbols, *, fetcher, config) -> dict[ccy, rate]`
Résout les taux pour **toutes les devises** présentes dans `symbols`. USD = 1.0.

**Priorité de résolution** (dégradation loud) :
1. **Fetch live** (yfinance, paire du `config`, `invert` si besoin).
2. **Fallback statique** (`config[ccy].fallback`) — avec `WARNING` (« fetch échoué/None,
   fallback statique X »).
3. **`1.0`** en dernier recours (devise sans config) — avec `WARNING` loud.

### `load_fx_config(path)`
Charge `config/fx.yaml`. Clés requises par devise : `yahoo` (paire à fetch) +
`fallback` (taux statique de secours).

## Où le taux s'applique (aval)

| Point | Usage du `fx_rate` |
|---|---|
| Sizing | `RiskGate.max_order_quantity_at_price(price, fx_rate=…)` |
| Risk gate | `order_value = |qty| × price × fx_rate` (bornes en USD) — cf. [risk-gate](risk-gate.md) |
| Fill | `fx_rate` **estampillé sur le fill** (P&L converti à ce taux) |
| Valorisation | valeur de position / equity / gross en USD |

## Ce qui reste natif (jamais converti)

Prix, barres, indicateurs, `hard_stop`/`take_profit`, thèse et rationale de
l'agent. Seules les **valeurs monétaires pour les bornes et le P&L** passent en USD.

## Voir aussi

- [Décisions](../decisions/registre-decisions-metier.md) · spec `docs/superpowers/specs/2026-06-24-fx-conversion-design.md`.
- [Risk gate](risk-gate.md) — consommateur principal du `fx_rate`.
