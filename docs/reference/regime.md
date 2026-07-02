# Référence — Régime : marché & familles

> **Type** : Reference (Diátaxis).
> **Code** : `trader/market/regime` (classifieur), `trader/market/family_regime` (biais familial) · **Décisions** : D2

Deux briques **déterministes** (0 LLM) : un classifieur de régime par symbole, et
un biais de régime cross-asset par famille thématique.

## Classifieur de régime — `market/regime`

`MarketRegime` : classification **déterministe** de l'état de marché d'un symbole à
partir de ses indicateurs. Composantes classées séparément :

| Composante | Fonction | Sortie |
|---|---|---|
| Régime directionnel | `_classify_regime(...)` | trending_up / down / range / breakout / unknown |
| Volatilité | `_classify_vol(vol)` | état de vol |
| Étirement | `_classify_stretched(z)` | `bool\|None` : sur-étendu si `\|z\| ≥ 2` |
| Bougie | `_classify_candle(signal)` | label de configuration de bougie |

Pur (indicateurs → labels), rejouable, sans dépendance temps/hasard.

## Biais familial — `market/family_regime` (D2)

Biais de régime **cross-asset par famille thématique** (énergie, défense, semis…) :

- `families_for_universe(symbols)` — regroupe les symboles par famille (**familles à
  ≥ 2 membres actifs seulement** ; une famille réduite à 1 symbole est écartée).
- `momentum_from_bars(bars, lookback_bars=3)` — momentum court d'une famille.
- `compute_family_bias(...)` — agrège en un biais par famille (haussier/baissier).

Sert de **contexte de régime** injecté à l'agent (une famille haussière renforce la
lecture d'un de ses membres). Frère long-horizon du radar (cf. [univers](universe-rotation.md)).

## Config

`regime.yaml` : `attribution_since`, `exclude_symbols` (cf. [config](config.md)).
Lu par `runtime/daemon` (+ cli, consolidator).

## Voir aussi
- [Univers & rotation](universe-rotation.md) · [Contexte agent](agent-context.md) · registre D2.
