# Référence — Risk Gate

> **Type** : Reference (Diátaxis) — ce que fait le composant *aujourd'hui*.
> **Code** : `trader/execution/risk.py` · **Config** : `config/risk.yaml`
> **Rôle** : fusible **déterministe** entre la décision du LLM et `broker.submit`.
> Aucune stratégie ici — que des bornes dures. Dernier rempart avant un ordre réel.

Le gate est **séparé** du modèle : le LLM propose, `RiskGate` valide, le broker
exécute. Un ordre approuvé par le LLM peut être **rejeté** par le gate ; un ordre
rejeté ne part jamais. Le gate est aussi le filet du **mode exploration** (quand
le gate de confiance côté agent est désactivé, les bornes dures restent).

## Contrat

```python
gate = RiskGate(RiskLimits.from_dict(risk_yaml))
verdict = gate.check(order, price, current_position_value=…, gross_exposure=…,
                     equity=…, allow_risk_reduction=…, fx_rate=…)
if verdict.approved:
    broker.submit(order)
```

`Verdict(approved: bool, code: str = "ok", context: str = "")` — **premier échec = rejet** (fail-fast).
`code` est un **code enum** stable (parsable), pas de la prose.

## Les 5 contrôles de `check()` (dans l'ordre d'évaluation)

| # | Code de rejet | Condition | Borne (`risk.yaml`) |
|---|---|---|---|
| 1 | `non_finite_order` | `order_value` NaN/inf | — (garde-fou : `NaN > x == False` contournerait tout) |
| 2 | `equity_floor_breached` | `equity < min_equity` | `min_equity` → **plus aucun ordre** sous ce plancher |
| 3 | `order_value_exceeded` | `order_value > max` (sauf risk-reducing) | `max_order_value` |
| 4 | `position_value_exceeded` | position projetée `> max` | `max_position_value` |
| 5 | `gross_exposure_exceeded` | brut projeté `> max` | `max_gross_exposure` |

`order_value = |qty| × price × fx_rate` (**FX-aware** : la valeur est bornée en
USD quel que soit la devise native — cf. chantier FX / incident Realtek).

### Exemption « risk-reducing »

Un ordre qui **réduit** une position opposée existante (sans la dépasser) est
**exempté de `order_value_exceeded`** quand `allow_risk_reduction=True`. Invariant :
on doit toujours pouvoir **couper** une position, même si sa valeur dépasse la
borne d'ouverture. Le contrôle nécessite : position opposée, taille ≤ position
actuelle, et position projetée < position actuelle.

## Gate de confiance — `check_confidence()` (séparé)

Rejette `confidence_below_required` quand la confiance de l'agent est sous le
seuil requis. Le seuil **scale linéairement avec le risque planifié** :

```
required = min_trade_confidence
         + (full_risk_confidence − min_trade_confidence)
           × clamp(planned_risk_pct / max_risk_per_trade_pct, 0, 1)
```

- risque nul → seuil plancher `min_trade_confidence` (défaut **0.7**).
- risque max (`max_risk_per_trade_pct`, défaut **1 %** de l'equity) → `full_risk_confidence`.

Autrement dit : plus tu risques, plus tu dois être confiant.

## Sizing (helpers, ne rejettent pas — informent)

- `max_order_quantity_at_price(price, fx_rate)` — quantité max telle que
  `order_value ≤ max_order_value` (FX-aware, boucle de sûreté anti-arrondi).
- `max_quantity_at_risk(…)` — quantité de référence telle que la perte au
  `hard_stop` ≤ `max_risk_per_trade_pct × equity`. Un dépassement est tracé en
  `risk_warnings`, sans bloquer l'ordre ; les fusibles notionnels restent dans
  `RiskGate.check()`.

## Config — `config/risk.yaml`

| Clé | Rôle | Défaut |
|---|---|---|
| `max_position_value` | $ max par position (abs) | — (requis) |
| `max_gross_exposure` | $ max exposition brute (Σ\|positions\|) | — (requis) |
| `max_order_value` | $ max par ordre unique | — (requis) |
| `min_equity` | plancher equity : sous ce seuil, zéro ordre | — (requis) |
| `max_risk_per_trade_pct` | % equity risqué si le hard_stop saute | `0.01` |
| `min_trade_confidence` | seuil confiance plancher (risque nul) | `0.7` |
| `full_risk_confidence` | seuil confiance au risque max | `0.9` |

`read_min_trade_confidence(path)` : lecture **fail-safe** de `min_trade_confidence`
(défaut 0.7 si fichier absent/illisible/clé invalide) — partagée daemon/tui/consolidator.

## Où c'est branché

`trader/runtime/daemon.py` orchestre : construit le `RiskGate` depuis `risk.yaml`,
appelle `check()` avant chaque `broker.submit`. Les **plans armés** (`EXECUTE_ORDER`)
passent aussi par ce gate à l'exécution (cf. [décisions D7B/D11](../decisions/registre-decisions-metier.md)).

## Voir aussi

- Décisions : D7B (plans armés), mode exploration basse-confiance (D15).
- [Architecture §3.7](../architecture.md) — validation & gates pré-exécution.
