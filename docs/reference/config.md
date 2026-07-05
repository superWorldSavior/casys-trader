# Référence — Configuration (`config/*.yaml`)

> **Type** : Reference (Diátaxis).
> **Fichiers** : `config/*.yaml` · **Lecteurs** : `trader/support/config/`, `trader/market/rotation/`, `trader/market/`, `trader/execution/`
> **Rôle** : toute la surface de config qui pilote le comportement, en un endroit.

Principe : le comportement se règle par **fichiers `.yaml` explicites** (pas de
defaults magiques cachés — AX). Cette page est la carte ; les fichiers riches ont
leur propre page de référence, liés ci-dessous.

## Carte des fichiers

| Fichier | Pilote | Champs clés | Lecteur |
|---|---|---|---|
| `universe.yaml` | **univers tradable live** (ce que le daemon analyse/trade) | `symbols` | `runtime/daemon`, `market/rotation/*` |
| `pool.yaml` | **pool Tier-1** source du radar + exclusions dures | `symbols`, `hard_exclusions` | `support/config/pool` (`load_pool`) |
| `portfolio.yaml` | capital de départ paper | `starting_cash` (100000) | `support/config/portfolio` (`load_starting_cash`) |
| `risk.yaml` | **bornes du risk gate** → voir [risk-gate](risk-gate.md) | `max_position_value` (30k), `max_gross_exposure`, `max_order_value`, `min_equity`, `max_risk_per_trade_pct` (0.01), `confidence_gate_enabled` (false), `require_hard_stop` (false) | `execution/risk` |
| `fx.yaml` | **taux FX** (paires + fallback) → voir [fx](fx.md) | par devise : `yahoo`, `invert`, `fallback` | `market/fx_rates` |
| `radar.yaml` | **rotation swing-aware** (D9/D10) | `cap_m` (25), `delta`, `dwell_days` (3), `score_window_bars` (15), `atr_floor`, `amplitude_cap`, `min_coverage`, `emergency_score` | `market/rotation/*`, `market/radar*` |
| `regime.yaml` | régime familial + fenêtre d'attribution | `attribution_since`, `exclude_symbols` | `runtime/daemon` (+ `runtime/cli`, `agent/learnings/consolidator`) |
| `data_sources.yaml` | **profil de données** (paper/prod) + routes de sources | `profile`, `profiles` | `market/data_source` |
| `sessions.yaml` | horaires/calendriers de séance par place | (par venue) | `market/rotation/schedule` (`load_sessions`) |
| `ib_contracts.yaml` | mapping symbole → contrat IB | `contracts` | `market/ib_source` |
| `symbol_names.yaml` | libellés d'affichage | (par symbole) | `reporting/read_models/runtime_state` (`_load_company_names`) |
| `conviction.yaml` | tilt de conviction **par famille** | (par famille) | `market/radar_config` (`load_conviction`) → `market/rotation/wiring` |

## Flags de comportement notables

Dans `risk.yaml` (mode exploration basse-confiance, D15) :
- `confidence_gate_enabled: false` — le gate de confiance **ne rejette plus** aucun
  ordre (l'agent explore ; les bornes dures restent le filet).
- `require_hard_stop: false` — ouverture **sans** hard_stop autorisée (bornée par le
  notionnel via `max_order_value`).

Dans `radar.yaml` : `cap_m` (taille max de l'univers), `dwell_days` (durée minimale
avant rotation d'un symbole), `score_window_bars` (horizon du score ≠ dwell).

## Piège univers

`universe.yaml` (tradable, éjecte les fermés) vs analyse swing (analyser les fermés
daily-valides) : même fichier, deux sens. Réconcilié par D13 (`analyzable_venues`).
Cf. registre D9/D10/D13.

## Voir aussi
- [risk-gate](risk-gate.md), [fx](fx.md) · registre D9/D10/D13 (univers/rotation).
