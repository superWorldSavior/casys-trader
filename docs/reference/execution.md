# Référence — Exécution : admission, budget gross, broker, portefeuille

> **Type** : Reference (Diátaxis).
> **Code** : `application/execute`, `application/portfolio/snapshot`, `domain/execution`, `domain/market/gross_priority`, `infrastructure/brokers`, `infrastructure/state_db`
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

## Broker / passage d'ordres

Modèle d'ordre + commissions.

| Type | Rôle |
|---|---|
| `domain/contracts.py` | `Order`, `Fill`, `Commission`, `Position` et nom du modèle |
| `application/execute/protocols.py` | ports `Broker` et `CommissionModel` attendus par l'application |
| `domain/execution/fill_accounting.py` | effet pur d'un fill sur position, prix moyen et cash |
| `infrastructure/brokers/commission_models.py` | modèles `none` et approximation IBKR |
| `infrastructure/state_db/sim_broker.py` | adaptateur paper JSON |
| `infrastructure/state_db/broker_store.py` | adaptateur paper SQLite canonique |

Le modèle de commission est sélectionné par config (`TRADER_COMMISSION_MODEL`,
défaut `ibkr`). Les frais rendent le P&L **net** (cf. conscience-frais, `be_ref_bps`).

`execution/broker`, `execution/commission`, `execution/contracts`,
`execution/protocols` et `tools/execution` sont des façades de compatibilité.
Les nouveaux imports internes visent directement le propriétaire indiqué dans
le tableau ci-dessus.

## Corrélation causale ordre → fill

Quand le pilote de processus est présent, le runtime ajoute à l'`Order` les
champs optionnels `process_instance_id`, `attempt_id` et `decision_id` après le
risk gate final. Le chemin direct et la tâche durable `execute_order` conservent
ces trois champs. `SqliteBroker` les recopie sur le `Fill` et dans
`broker_fills` au sein de la même transaction que les mutations de cash et de
position.

Au retour, le runtime compare le `symbol` et les trois identités du fill à la
décision courante. Un fill absent, ou un fill portant une autre corrélation, ne
peut pas produire `executed=true` : l'effet devient `unknown` et l'instance
reste en `recovery_required`. Un fill valide alimente au contraire les
`effect_refs` `broker_fill` et `portfolio_readback` de la décision.

En dry-run, aucun fill durable n'est attendu ; la non-application est attestée
par un reçu explicite `execution_mode=dry_run`. Les champs étant optionnels et
omis lorsqu'ils valent `None`, les ordres et fills historiques conservent leur
forme JSON.

## Portefeuille

`domain/portfolio/snapshot.py` porte `Holding`, `Snapshot` et leurs agrégats purs.
`application/portfolio/snapshot.py` assemble cette vue depuis le port minimal
`PortfolioReader`, les prix et les FX injectés. La valorisation reste **en USD**
(`quantity × last_price × fx_rate`, cf. [fx](fx.md)).

`execution/portfolio` et `tools/portfolio` conservent les imports historiques,
mais ne portent plus de logique.

## Voir aussi
- [Gouvernance du processus](process-governance.md) (preuve de bout en bout) ·
  [Risk gate](risk-gate.md) (fusible en amont) · [FX](fx.md) ·
  [reporting](reporting.md).
