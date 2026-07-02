# Référence — Gestion d'univers (radar & rotation)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/rotation/` (core, wiring, venues, schedule, override, collectors, state), `trader/market/radar*` · **Config** : `pool.yaml`, `radar.yaml`, `universe.yaml`
> **Décisions** : D9 (univers = tradable), D10 (rotation/hot-sets par place), D13 (analyzable)

L'univers **live** n'est pas figé : un radar daily (0 LLM) score le pool, une
rotation à **hystérésis** sélectionne les symboles chauds, et le résultat pilote
`universe.yaml`.

## Radar — Tier-1 daily (`market/radar*`)

Score d'attractivité **daily, sans appel LLM**, sur le pool (`pool.yaml` : `symbols`
+ `hard_exclusions`). Params dans `radar.yaml` (16 clés), dont : `cap_m` (taille max
univers), `score_window_bars` (~3 sem.), poids composites `w_trend`/`w_rs`/`w_amp`,
`atr_floor`, `amplitude_cap`, `gap_threshold`, `min_coverage`, `emergency_score`,
`benchmarks`/`default_benchmark`, `delta`, `dwell_days`, `override_enabled`,
`preopen_window_minutes`.

## Rotation à hystérésis — `rotation/core.apply_hysteresis(...)`

Sélectionne ≤ `cap_m` symboles chauds en **préservant les incumbents** (évite le
churn) :

- Les **incumbents** (déjà chauds) sont gardés, triés par attractivité, tronqués à
  `cap_m`.
- Un **entrant** ne remplace un incumbent que si son avantage d'attractivité dépasse
  `delta` (swap conditionnel).
- Un incumbent n'est **évictable qu'après `dwell_days`** jours chaud (`dwell` compte
  les jours par symbole).

## Modules de rotation

| Module | Rôle |
|---|---|
| `core` | logique hystérésis / swap conditionnel (ci-dessus) |
| `wiring` | point d'entrée CLI prod (`run_cli`), constructeur de l'override LLM (`build_llm_override_fn`), câblage du rank fn (importe `load_conviction` de `market/radar_config`) |
| `venues` | état de rotation **par place** (hot-sets par venue, D10) |
| `schedule` | **gate de déclenchement** : rotation à la clôture de session ; `analyzable_venues()` (D13) |
| `override` | **override LLM** de la rotation — surcharge tracée du `default_hot` |
| `collectors` | collecte des symboles « sticky » + override par défaut |
| `state` | état persistant de l'hystérésis (mémoire inter-cycles) |

## Le piège tradable vs analyzable (D13)

Deux besoins opposés sur le même `universe.yaml` :
- **D9/D10** : univers = **tradable** (éjecte les places fermées).
- **Swing** : analyser aussi les **fermés daily-valides**.

Réconcilié par **D13** : `analyzable_venues()` (`rotation/schedule:101`) =
**open ∪ preopen**. Attention : D13 ne couvre que la fenêtre pré-open, pas toute la
fermeture (cf. registre D9/D10/D13, note live 17/06).

## Voir aussi
- [Config](config.md) (`pool.yaml`, `radar.yaml`, `universe.yaml`) · registre D9/D10/D13.
- Override rotation : **actif** en prod — `build_llm_override_fn` (`rotation/wiring`) est branché quand `radar.yaml override_enabled: true`, et passé à `venues.tick()`. (Le code mort, c'est `rotation/daemon.maybe_rotate()` — jamais appelé par le daemon prod.)
