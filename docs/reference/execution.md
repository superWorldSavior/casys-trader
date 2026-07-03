# Référence — Exécution : admission, budget gross, broker, portefeuille

> **Type** : Reference (Diátaxis).
> **Code** : `application/order_admission`, `market/gross_priority`, `tools/execution`, `tools/portfolio`
> **Rôle** : le chemin d'un ordre approuvé jusqu'au fill, et la vue portefeuille.

Après la décision LLM et le [risk gate](risk-gate.md), un ordre passe par :
admission (helpers purs) → ordre d'exécution gross-fair → broker → portefeuille.

## Admission — `application/order_admission` (helpers purs)

Aucune I/O, aucune orchestration (le daemon orchestre) — juste des fonctions pures
d'aide à la décision d'ordre :

| Fonction | Rôle |
|---|---|
| `invalid_intent_reason(action, quantity, intent)` | valide l'`intent` (OPEN_LONG/SHORT, CLOSE, REVERSE…) **vs l'action** (BUY/SELL) |
| `hard_stop_price(raw_exit_plan)` | extrait le prix de hard_stop du plan de sortie |
| `hard_stop_wrong_side(intent, entry, stop)` | garde : stop du mauvais côté (long avec stop au-dessus…) |
| `reverse_open_quantity(action, quantity, position_quantity)` | quantité d'ouverture après un REVERSE |
| `risk_pct_for_quantity(quantity, stop_distance, equity)` | % equity risqué (distance au stop) |
| `set_entry_risk_metrics(...)` | pose les métriques de risque d'entrée sur la décision |

## Budget gross — `market/gross_priority`

`gross_execution_order(items: list[PriorityItem]) -> list[str]` : **ordre
d'exécution déterministe** des décisions pour une admission gross **équitable** —
quand plusieurs ordres se présentent au même cycle et que le plafond d'exposition
brute (`max_gross_exposure`) est contraint, l'ordre de passage est fixé (pas de
biais d'itération). Cf. historique `git log` (design gross-budget-allocator livré).

## Broker / passage d'ordres — `tools/execution`

Modèle d'ordre + commissions.

| Type | Rôle |
|---|---|
| `Order` | ordre (`symbol`, `side`, `quantity`, `rationale`) |
| `Fill` | exécution (prix, quantité, `fx_rate` estampillé) |
| `Commission` / `CommissionModel` (Protocol) | modèle de frais |
| `NoCommissionModel` | frais nuls (test) |
| `IbkrCommissionModel` | barème IBKR (planchers par place, cf. incident TW/Realtek) |

Le modèle de commission est sélectionné par config (`TRADER_COMMISSION_MODEL`,
défaut `ibkr`). Les frais rendent le P&L **net** (cf. conscience-frais, `be_ref_bps`).

## Portefeuille — `tools/portfolio`

`snapshot(...) -> Snapshot` : vue **agrégée** — positions valorisées (`Holding`),
équité, cash, P&L net, KPI. Valorisation **en USD** (`quantity × last_price ×
fx_rate`, cf. [fx](fx.md)). Consommé par le cockpit et le contexte agent.

## Voir aussi
- [Risk gate](risk-gate.md) (fusible en amont) · [FX](fx.md) · [reporting](reporting.md).
