# How-to — Mesurer & rejouer

> **Type** : How-to (Diátaxis). Deux outils de **mesure lecture-seule**, jamais de
> validation a priori (l'agent reste libre — décision Erwan, registre D7/D8).

## Mesurer l'activité décisionnelle — `scripts/measure_d7.py`

Compte les appels LLM décideur, le gate, les plans armés **depuis un instant
donné** (lecture seule sur `state/events.jsonl` + `state/decisions.jsonl`).

```bash
uv run python scripts/measure_d7.py --since 2026-06-11T04:53:00+00:00
```

- `--since` : ISO 8601 (typiquement le `ts` d'un redéploiement).
- **Référence pré-D7** : 92 appels LLM décideur / 24 h (mesuré le 2026-06-11).
  Cible D7/D8 : réduire ce débit (voir registre D7/D8).

Usage typique : après un redéploiement, mesurer l'impact d'un changement sur la
cadence d'appels, les raisons de décision et les événements de plans armés, sur
une fenêtre comparable.

## Rejouer des plans armés — `backtest/plan_replay.py`

Un plan armé (condition + sens + taille + stop + TTL) est **100 % déterministe** :
on le rejoue sur des barres historiques **sans appel LLM**, avec les MÊMES briques
que le live (évaluation de watch, cohérence prix/stop, moteur de sortie).

Outil de **mesure de la qualité des scénarios** de l'agent — a posteriori. Sert à
répondre empiriquement à des questions comme « les plans armés mécaniquement
valides mais à thèse morte perdent-ils de l'argent ? » (cf. registre D12, écarté
faute de signal).

`backtest.plan_replay` est aujourd'hui une **API Python**, pas une CLI : exécuter
`python -m backtest.plan_replay` ne rejoue rien. Le point d'entrée est :

```python
from backtest.plan_replay import replay_armed_plan

result = replay_armed_plan(watch, bars)
print(result)
```

`watch` est le dictionnaire durable d'un plan armé et `bars` une liste de
`Bar` ordonnée. `PlanReplayResult` distingue `expired`,
`cancelled:stop_incoherent`, `invalid` et `executed`, avec trigger, sortie et
`pnl_pct` lorsque disponibles.

Pour vérifier le contrat et voir des fixtures complètes :

```bash
uv run pytest -q tests/test_plan_replay.py
```

Il n'existe pas encore de commande qui charge automatiquement un plan depuis
`state/` et télécharge ses barres. Pour une analyse réelle, préparer ces deux
entrées dans un script ponctuel sans modifier le ledger.

## Quand utiliser quoi

| Question | Outil |
|---|---|
| « Mon changement a-t-il réduit les appels LLM ? » | `measure_d7.py --since <redeploy>` |
| « Les scénarios armés de l'agent étaient-ils bons ? » | API `replay_armed_plan(watch, bars)` |
| « Combien de plans armés ont tiré / expiré ? » | `measure_d7.py` + events `armed_plan_*` |

## Voir aussi
- [Lire les logs](read-logs.md) · registre décisions D7, D8, D12.
