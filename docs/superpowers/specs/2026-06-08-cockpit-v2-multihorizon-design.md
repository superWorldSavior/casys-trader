# Cockpit v2 — signaux pré-calculés multi-horizon

**Date** : 2026-06-08
**Statut** : design validé, exécution en **pair-codex** (Opus orchestre, Codex tape)

---

## 1. Contexte & problème

Le cockpit poussé à l'agent décideur (`build_market_cockpit`, `trader/agent_context.py`)
calcule tout sur **une seule temporalité** (15m runtime). Deux constats de cette
session :
- Les patterns **chartistes et bougies** (et les indicateurs) sont du bruit en 15m,
  du signal en 1h/4h/1d. Tout outil sérieux fait du **top-down multi-horizon**.
- L'agent **ne pull pas** (`REQUEST_CONTEXT` multi-TF existe mais inutilisé — cf
  [[casys-trader-finding-watch-levers]]). Donc il faut **pousser**.

## 2. Objectif

Enrichir le cockpit avec des signaux **pré-calculés multi-horizon** qui *méritent
d'être regardés*, **sans alourdir** et **sans retirer l'analyse à l'agent** : on
attire son attention sur le notable, il garde le jugement et la décision.

## 3. Décisions (validées)

- **Horizons** : `15m` (base) + `1h` + `4h` + `1d`.
  - `1h`/`4h` dérivés des barres 15m déjà fetchées via `market.aggregate_bars`
    (zéro fetch supplémentaire).
  - `1d` = barres journalières **fetchées séparément** (le lookback intraday ne
    suffit pas) et fournies au cockpit.
- **Format = hybride** (compact par construction) :
  - `htf` : régime de la temporalité la plus haute disponible (`1d`, sinon `4h`)
    — la tendance de fond. Toujours présent.
  - `aligned` : booléen — la direction du régime 15m va-t-elle dans le sens de
    `htf` ? Toujours présent.
  - `sig` : liste de signaux **saillants** `"{horizon}:{signal}"` — **présent
    seulement si non vide** (rien à signaler = pas de champ).

## 4. Format de sortie (par symbole, dans le cockpit)

```json
"NVDA": { ...colonnes base 15m (cp2)...,
  "htf": "trending_up",
  "aligned": true,
  "sig": ["1d:breakout_up", "4h:stretched_up", "15m:bull_engulf"] }

"SPY":  { ...colonnes base..., "htf": "range", "aligned": false }
        // rien de saillant -> pas de champ "sig"
```

### Grammaire de `sig` — ce qui est « saillant » (réutilise les indicateurs existants)
Pour chaque horizon, émettre un signal **uniquement** si :
- `chart_breakout != 0` → `breakout_up` / `breakout_down`
- `candlestick_signal` = pattern non neutre → `bull_engulf` / `bear_engulf` /
  `hammer` / `shooting_star` (mapping de `regime.CANDLE_SIGNAL_LABELS`)
- `|z_score| >= regime.Z_STRETCHED_THRESHOLD` → `stretched_up` / `stretched_down`

Pas de signal de régime par horizon dans `sig` (le `htf` porte déjà la tendance de
fond) → on garde `sig` réservé aux **événements** ponctuels.

### `aligned`
`True` si le régime base (15m) et `htf` pointent la **même direction**
(`trending_up`+`trending_up`, ou `trending_down`+`trending_down`). Sinon `False`
(divergence, `range`, `breakout` ou `unknown` d'un côté).

## 5. Architecture

- **Réutilise `trader/regime.py`** (`classify_regime`) pour le régime par horizon —
  pas de nouvelle logique de classification.
- **Nouvelle fonction pure** (ex. `trader/regime.py` ou `trader/cockpit_signals.py`) :
  `multi_horizon_signals(bars_by_horizon: dict[str, list[Bar]], *, window) -> dict`
  → `{ "htf", "aligned", "sig" }`. Calcule indicateurs + régime par horizon, applique
  la grammaire ci-dessus. Déterministe, fail-safe (horizon manquant ignoré ; jamais
  d'exception ; `sig` omis si vide).
- **`build_market_cockpit`** :
  - dérive `1h`/`4h` via `aggregate_bars` depuis les barres 15m reçues ;
  - accepte un nouveau paramètre **optionnel** `daily_bars_by_symbol` pour le `1d` ;
  - ajoute `htf`/`aligned`/`sig` par ligne symbole ; bump version `cp2`→`cp3` ;
    met à jour `schema`.
- **Daemon** (`trader/daemon.py`) : fetch les barres `1d` par symbole (via
  `market.get_bars(sym, lookback adapté, "1d")`, fail-safe) et les passe à
  `build_market_cockpit`. Si le fetch 1d échoue/stale → on continue sans 1d
  (`htf` retombe sur `4h`), jamais de crash.

## 6. Invariants à tester (TDD)
1. `multi_horizon_signals` : `htf` = régime de l'horizon le plus haut fourni.
2. `aligned` correct (même direction → True ; divergence/range → False).
3. `sig` n'émet QUE le notable ; calme total → `sig` absent.
4. Mapping signaux : breakout/bougie/stretched corrects par horizon, avec direction.
5. Fail-safe : horizon manquant, barres vides, indicateur None → pas de crash, pas
   de signal inventé.
6. `aggregate_bars` 15m→1h→4h cohérent (réutilise l'existant ; test d'intégration
   cockpit).
7. Cockpit `cp3` : nouveaux champs présents, colonnes/indices base inchangés
   (pas de régression `rank_by_abs` ni des consumers `cols.index(...)`).
8. Daemon : fetch 1d échoué → cockpit construit quand même (`htf` sur 4h).

## 7. Hors scope (YAGNI / itérations suivantes)
- **macro / micro** explicites (mentionnés mais non définis) → itération suivante.
- Changement du **contrat de sortie** LLM / champ de raisonnement → séparé.
- Migration runtime IB (chantier distinct).
- Le pull `REQUEST_CONTEXT` reste inchangé (complément, pas remplacé).

## 8. Process
Exécution **pair-codex** (Codex implémente en TDD sous brief Opus), review Codex
pré-commit, `uv run pytest -q` vert.
