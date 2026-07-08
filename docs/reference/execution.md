# Référence — Exécution : admission, budget gross, broker, portefeuille

> **Type** : Reference (Diátaxis).
> **Code** : `application/execute/order_admission`, `application/execute/risk_admission`, `domain/market/gross_priority`, `execution/broker`, `execution/portfolio`
> **Rôle** : le chemin d'un ordre approuvé jusqu'au fill, et la vue portefeuille.

Après la décision LLM, un ordre passe par :
admission intent/exit → admission risque + [risk gate](risk-gate.md) final →
ordre d'exécution gross-fair → broker → portefeuille.

## Admission intent/exit — `application/execute/order_admission` (helpers purs)

Aucune I/O, aucune orchestration runtime — juste des fonctions pures d'aide à la
décision d'ordre :

| Fonction | Rôle |
|---|---|
| `invalid_intent_reason(action, quantity, intent)` | valide l'`intent` (OPEN_LONG/SHORT, CLOSE, FLIP…) **vs l'action** (BUY/SELL) |
| `hard_stop_price(raw_exit_plan)` | extrait le prix de hard_stop du plan de sortie |
| `hard_stop_wrong_side(intent, entry, stop)` | garde : stop du mauvais côté (long avec stop au-dessus…) |
| `flip_open_quantity(action, quantity, position_quantity)` | quantité d'ouverture après un FLIP |
| `risk_pct_for_quantity(quantity, stop_distance, equity)` | % equity risqué (distance au stop) |
| `set_entry_risk_metrics(...)` | pose les métriques de risque d'entrée sur la décision |

## Admission risque — `application/execute/risk_admission`

`assess_risk_admission(request, gate=...)` regroupe l'admission risque des
ouvertures sans prendre d'effet durable. `assess_final_risk_gate(request,
gate=...)` construit l'`Order` et applique le `RiskGate.check(...)` final :

| Élément | Rôle |
|---|---|
| `RiskAdmissionRequest` | contexte runtime minimal : action/intent, quantité, prix, equity, position, stop, FX, confiance |
| `RiskAdmissionGate` | `Protocol` local exposant `max_quantity_at_risk(...)`, `check_confidence(...)` et `limits.max_risk_per_trade_pct` |
| `RiskAdmissionResult` | verdict, quantité possiblement dérivée, champs de décision à persister, raison/contexte de rejet |
| `FinalRiskGateRequest` | contexte du gate final : ordre, prix, position, gross, equity, FX |
| `FinalRiskGate` | `Protocol` local exposant `check(...)` |
| `FinalRiskGateResult` | `Order` exécutable + verdict/reason/context du gate final |

Le daemon conserve le logging, le scheduling, l'écriture `decisions.jsonl`, le
broker et les effets sur les plans.

## Budget gross — `domain/market/gross_priority`

`gross_execution_order(items: list[PriorityItem]) -> list[str]` : **ordre
d'exécution déterministe** des décisions pour une admission gross **équitable** —
quand plusieurs ordres se présentent au même cycle et que le plafond d'exposition
brute (`max_gross_exposure`) est contraint, l'ordre de passage est fixé (pas de
biais d'itération). Cf. historique `git log` (design gross-budget-allocator livré).

Sous l'itération libre (streaming, cf. [task-queue](task-queue.md)), cet ordre
s'applique à la **phase des ouvertures** : les sorties (`CLOSE`/`REDUCE`) sont
exécutées au fil de l'eau (elles libèrent de la marge), puis les ouvertures
bufferisées passent par `gross_execution_order` en voyant la marge refreshée →
l'arbitrage au mérite (réducteurs d'abord, puis conviction décroissante) est
préservé. Le budget des ouvertures est le RiskGate/marge : le cap de débit
`max_orders_per_cycle` a été retiré (redondant avec les bornes $, qui restent la
safety capital).

## Broker / passage d'ordres — `execution/broker`

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

`tools/execution` reste une façade de compatibilité pour les imports historiques.
Les imports internes nouveaux doivent viser `execution/broker`.

## Portefeuille — `execution/portfolio`

`snapshot(...) -> Snapshot` : vue **agrégée** — positions valorisées (`Holding`),
équité, cash, P&L net, KPI. Valorisation **en USD** (`quantity × last_price ×
fx_rate`, cf. [fx](fx.md)). Consommé par le cockpit et le contexte agent.

## Voir aussi
- [Risk gate](risk-gate.md) (fusible en amont) · [FX](fx.md) · [reporting](reporting.md).
