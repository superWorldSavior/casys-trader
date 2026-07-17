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
| `risk.yaml` | **bornes du risk gate** → voir [risk-gate](risk-gate.md) | `max_position_value` (10k), `max_gross_exposure`, `max_order_value`, `min_equity`, `max_risk_per_trade_pct` (0.01), `confidence_gate_enabled` (false), `require_hard_stop` (false) | `execution/risk` |
| `fx.yaml` | **taux FX** (paires + fallback) → voir [fx](fx.md) | par devise : `yahoo`, `invert`, `fallback` | `infrastructure/market_sources/fx_rates` |
| `radar.yaml` | **rotation swing-aware** (D9/D10/D15) | `cap_m` (25), `delta`, `dwell_days` (3), `score_window_bars` (15), `atr_floor`, `amplitude_cap`, `min_coverage`, `emergency_score` | `market/rotation/*`, `market/radar*` |
| `regime.yaml` | régime familial + fenêtre d'attribution | `attribution_since`, `exclude_symbols` | `runtime/daemon` (+ `runtime/cli`, `agent/learnings/consolidator`) |
| `data_sources.yaml` | **profil de données** (paper/prod) + routes de sources | `profile`, `profiles` | `infrastructure/market_sources/data_source` |
| `sessions.yaml` | horaires/calendriers de séance par place | (par venue) | `market/rotation/schedule` (`load_sessions`) |
| `ib_contracts.yaml` | mapping symbole → contrat IB | `contracts` | `infrastructure/market_sources/ib_source` |
| `symbol_names.yaml` | libellés d'affichage | (par symbole) | `reporting/read_models/runtime_state` (`_load_company_names`) |
| `symbol_news_aliases.yaml` | alias d'entités pour l'attribution challenger | alias par symbole | `runtime/news_challenger_runtime` |
| `conviction.yaml` | tilt de conviction **par famille** | (par famille) | `market/radar_config` (`load_conviction`) → `market/rotation/wiring` |

## Flags de comportement notables

Dans `risk.yaml` (mode exploration basse-confiance) :

- `confidence_gate_enabled: false` — le gate de confiance **ne rejette plus** aucun
  ordre (l'agent explore ; les bornes dures restent le filet).
- `require_hard_stop: false` — ouverture **sans** hard_stop autorisée (bornée par le
  notionnel via `max_order_value`).

Dans `radar.yaml` : `cap_m` (25 non-sticky maximum dans la hotlist),
`dwell_days` (durée minimale avant rotation d'un symbole), `score_window_bars`
(horizon du score ≠ dwell). Le pool candidat amont reste distinct : top 40 radar
ainsi que tous les challengers fresh-news qualifiés.

`w_trend`, `w_rs` et `w_amp` pilotent encore exclusivement le score de
production `legacy_raw_v1`. Des valeurs identiques ne signifient pas une
influence identique, car les composantes ne partagent pas la même échelle. Le
score `balanced_percentile_v1` est volontairement non configurable et shadow :
percentiles par venue, 50 % trend, 50 % force relative alignée, amplitude comme
filtre d'éligibilité uniquement. Il écrit son audit mais ne modifie ni le top 40,
ni le `candidate_scope_id`, ni la hotlist.

Kill switches runtime D15, activés par défaut :

- `CASYS_NEWS_MACRO_ANALYST_ENABLED=0` désactive la production async des briefs ;
- `CASYS_UNIVERSE_INTELLIGENCE_ENABLED=0` désactive la préparation async de la
  hotlist par l'agent univers.

Le profil LLM univers est séparé du brain symbole (`casys-trader:universe-agent`)
et peut être réglé avec `TRADER_UNIVERSE_MODEL`,
`TRADER_UNIVERSE_ACPX_AGENT`, `TRADER_UNIVERSE_ACPX_BIN` et
`TRADER_UNIVERSE_ACPX_SESSION_LABEL`.

Le brain trader (décideur par symbole) se règle de la même façon avec
`TRADER_MODEL` et `TRADER_ACPX_AGENT` — ces knobs ne s'appliquent qu'au profil
trader par défaut, jamais aux rôles analystes (rotation, universe) qui partagent
le même builder avec un modèle explicite.

Dans les deux cas, la rotation reste fail-open et utilise sa baseline
déterministe. Réactiver le code ne backfill pas un ancien état : il faut un daemon
actif et attendre la prochaine clôture puis le prochain pré-open de chaque venue
pour matérialiser le parent, le scope final, le brief et le run correspondants.

## Piège univers

`universe.yaml` (tradable, éjecte les fermés) vs analyse swing (analyser les fermés
daily-valides) : même fichier, deux sens. Réconcilié par D13 (`analyzable_venues`).
Cf. registre D9/D10/D13/D15.

## Voir aussi
- [risk-gate](risk-gate.md), [fx](fx.md) · registre D9/D10/D13/D15 (univers/rotation).
