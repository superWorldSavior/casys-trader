# casys-trader — Design

> Auteur : Erwan + Claude
Statut : LIVRÉ — 2026-07-02 — toutes les briques du design initial (daemon, tools/, codex_client, risk gate, indicator_watch, exit_plan/trade_plan, semantic layer, backtest maison) implémentées et testées en prod.


## 1. Intention

Construire un **agent de trading autonome** qui observe les marchés, décide quand
se réveiller, définit lui-même ses indicateurs et sa stratégie, et passe des
ordres en **paper trading**. Le LLM décideur est **Codex**, appelé
programmatiquement.

Principe directeur : **je (Claude) ne code PAS la stratégie, les indicateurs, ni
le calendrier de réveil.** Je construis l'**infrastructure** (outils, mandat,
sécurité, mémoire, harnais) ; l'agent runtime possède sa stratégie et la fait
évoluer selon ses KPI.

## 2. Les deux boucles

### Boucle 1 — Dev/design (humaine, lente)
`Erwan + Claude`, en conversation **dans le repo**. On met en place et on fait
évoluer : le mandat, les outils, les skills, le comportement de l'agent, les
marchés. On éprouve une stratégie sur l'historique via le **backtest maison**
(`backtest/`) avant de la déployer.

> Note (2026-06-06) : LEAN était le plan initial mais a été **abandonné** (offre
> backtest payante). Remplacé par un moteur de backtest maison léger
> (`backtest/` : data + engine + metrics + CLI), sans Docker ni dépendance
> externe payante.

### Boucle 2 — Runtime (autonome, rapide)
Un **daemon** lancé par un script. Le brain Codex est appelé programmatiquement :
il réveille les symboles dus, lit le marché via ses outils, décide, passe l'ordre
(après le risk gate), écrit ses learnings en mémoire, et peut override le prochain
réveil de chaque symbole.

**Lien entre les boucles** : `mandate/`, `memory.md`, `skills/` et `config/` sont
des **fichiers du repo**. La boucle 1 les édite, la boucle 2 les lit au réveil.
Le repo *est* l'interface partagée.

## 3. Partage des responsabilités

| Je construis (infra figée) | L'agent possède (non codé par moi) |
|---|---|
| La boîte à outils (data, exécution, portefeuille/PnL/KPI) | **Quand** se réveiller |
| Le scheduler que l'agent pilote | **Quels** indicateurs / signaux |
| La mémoire persistante | Sa **stratégie** et ses décisions |
| Le mandat (objectif + marchés) | Son organisation selon ses KPI |
| Le risk gate (fusible de sécurité) | Ses propres règles de risque internes |

## 4. Architecture

### 4.1 Daemon runtime (`trader/`) — léger, tourne maintenant
Boucle : `symboles dus → contexte → Codex → risk gate → exécution → log → timer global, override symbole ou veille indicateur`.

- **Python pur**, pas de dépendance Docker pour trader.
- Data légère (yfinance pour la v1) derrière l'interface `market.py`.
- Exécution **simulée** (fill simulator paper) en v1, derrière `execution.py` —
  swappable vers IB (`ib_async` ou LEAN) plus tard **sans changer l'algo**.

### 4.2 Boîte à outils de l'agent (`trader/tools/`)
Primitives composables, contrats étroits :
- `market.py` — lire données marché (prix, barres, lookback configurable)
- `execution.py` — placer un ordre (sim paper now → IB après, **même interface**)
- `portfolio.py` — positions, cash, PnL, KPI de performance
- `scheduler.py` — timer global par défaut + overrides par symbole
- `memory.py` — lire/écrire la stratégie et les learnings persistants

### 4.2 ter Veilles indicateurs (`trader/indicator_watch.py`)
Le scheduler ne doit pas seulement dormir jusqu'à une heure fixe. Codex peut
poser une `indicator_watch` temporaire pour un symbole : logique `all|any`,
conditions `{symbol, indicator, op, value, interval, window}`, horizon
`ttl_minutes`, et `on_trigger=WAKE|WAKE_WITH_ORDER_INTENT`.

Le daemon évalue ces watches au poll sans appel modèle, sur des timeframes
bornées (`15m`, `30m`, `1h`, `1d`). Si la combinaison déclenche, la watch est
one-shot : elle est retirée, le symbole devient dû immédiatement, et l'événement
est injecté dans `context.indicator_triggers`. Si
`on_trigger=WAKE_WITH_ORDER_INTENT`, le trigger transporte une intention d'ordre,
mais le runtime repasse par Codex + risk gate au réveil, pour garder un contexte
frais et un seul chemin d'exécution d'ordre.

### 4.2 bis Semantic layer trading (`trader/semantic/`, `trader/features.py`)
Le modèle à reprendre de GeoNexus est la **semantic layer gouvernée**, pas le
transport MCP. Pour le trader, l'interface opérable est un **CLI JSON stable** et
des fonctions Python importables par le daemon.

Principe : Codex ne calcule pas mentalement les indicateurs depuis les barres.
Le code expose un catalogue versionné d'indicateurs, périodes, familles et
features calculables ; Codex choisit les signaux utiles pour sa situation et
raisonne sur des résultats déterministes. Le prompt initial est volontairement
compact : `context.cockpit` contient `cols` + `rows` avec colonnes courtes
(`r`, `vol`, `z`, `er`, `ac`, `rs`, `sz`), sans OHLCV brut. Le CLI sert de contrat
humain/test :

Les indicateurs gouvernés couvrent le statistique/cross-asset et des signaux
price-action compacts : chandeliers japonais (`candlestick_signal`,
`candle_body_ratio`, `candle_wick_skew`) et chartisme (`chart_breakout`,
`trend_slope`, `range_position`). Ces signaux restent numériques pour être
utilisables par `REQUEST_CONTEXT`, `indicator_watch` et les tests.

L'axe temporel est un axe gouverné du cube sémantique :
`symbol × indicator × timeframe × lookback × window × as_of`. Les timeframes
canoniques sont `15m`, `30m`, `1h`, `4h`, `1d`; `4h` est exposé comme timeframe
sémantique mais calculé depuis `source_interval=1h`.

```
casys-trader semantic describe --json
casys-trader indicators list --json
casys-trader indicators get --symbol SPY --timeframe 4h --lookback 1mo --window 48 --names efficiency_ratio,z_score,volatility --json
casys-trader indicators compare --family energy --timeframe 1h --metrics relative_strength,spread_zscore --json
```

Le daemon importe les mêmes fonctions directement plutôt que de lancer le CLI en
subprocess. La boucle agentique fait deux étapes bornées :
`Codex -> REQUEST_CONTEXT optionnel -> calculs déterministes -> Codex -> décision`.
Des limites runtime (`max_context_requests_per_symbol`,
`max_indicators_per_request`, `max_model_calls_per_cycle`) empêchent une dérive de
coût.

### 4.3 Decision brain (`trader/codex_client.py`)
Appel **programmatique** via une API de transport LLM agnostique. Le transport
primaire reste Codex Spark via `acpx` headless ; un fallback OpenAI-compatible
Ollama Cloud peut être configuré par `.env` (`TRADER_OLLAMA_API_KEY`,
`TRADER_OLLAMA_BASE_URL`, `TRADER_OLLAMA_MODEL`, par défaut
`nemotron-3-nano:30b-cloud`). Le fallback ne s'active que sur erreur fournisseur
retryable : quota/crédit, 429, timeout, indisponibilité fournisseur. Une réponse
métier invalide ne déclenche pas un changement de modèle : elle devient `HOLD`.

Le brain reçoit le contexte JSON, force une **sortie structurée** (action,
taille/poids, confidence, rationale, override optionnel
`next_wake_in_minutes` pour ce symbole, et watch optionnelle
`indicator_watch`). Chaque décision conserve `llm_provider`, `llm_model` et
`llm_fallback_reason`. Chaque fill exécuté alimente
`state/model_performance.jsonl` pour agréger les performances par modèle dans
`trader.stats`.

La console doit rester observable : chaque cycle loggue le chargement marché, le
symbole courant, les appels modèle, les demandes de contexte, les décisions, les
ordres, les blocages risk gate et le prochain sommeil. En parallèle, le daemon
écrit `state/daemon_status.json`, `state/current_report.json` et
`state/events.jsonl` pendant le cycle.

Le démarrage live respecte le scheduler existant : si un symbole a déjà un
`next_wake` futur, il n'est pas réanalysé au relaunch. Le scan complet au
démarrage devient explicite via `--bootstrap-all`, utile pour reset manuel ou
diagnostic, mais pas par défaut.

`acpx --format json` ne fournit qu'une enveloppe JSON de transport ; il ne force
pas la réponse métier à respecter un schema `Decision`. Le daemon doit donc
continuer à parser, valider et normaliser les formats JSON courants du modèle
avant exécution, et bloquer tout ce qui reste inexploitable.

Pour une ouverture ou un reverse, Codex peut fournir un `exit_plan`. Ce plan est
une intention de trading structurée, pas un ordre libre :

```
{
  "intent": "OPEN_LONG",
  "action": "BUY",
  "quantity": 10,
  "exit_plan": {
    "hard_stop": { "type": "price", "price": 95.0 },
    "take_profits": [
      { "name": "tp1", "price": 105.0, "fraction": 0.5, "after_fill": "move_stop_to_breakeven" },
      { "name": "tp2", "price": 110.0, "fraction": 0.5, "after_fill": "close" }
    ],
    "profit_protection": {
      "arm_at_r": 0.5,
      "trigger_on_giveback_pct": 0.4,
      "close_fraction": 0.33,
      "move_stop_to": "breakeven",
      "min_hold_minutes": 10
    },
    "exit_watch": {
      "ttl_minutes": 180,
      "cooldown_minutes": 15,
      "logic": "any",
      "conditions": [
        { "indicator": "trend_slope", "op": ">", "value": 0, "timeframe": "15m", "window": 24 },
        { "indicator": "candlestick_signal", "op": ">", "value": 0.5, "timeframe": "15m", "window": 12 }
      ]
    },
    "trailing_stop": { "enabled_after": "tp1", "trail_type": "price", "trail_value": 2.0 },
    "max_hold_minutes": 45
  }
}
```

L'agent définit la thèse, les seuils et les fractions. L'infra persiste le plan
dans `state/trade_plans.json` et l'applique à chaque poll : stop dur, TP1/TP2,
déplacement du stop à breakeven, protection progressive opt-in, `exit_watch`,
trailing stop et sortie temps. `profit_protection` n'est jamais inventé par le
daemon : il est appliqué seulement si l'agent l'a mis dans le plan. `exit_watch`
est une veille indicateur d'invalidation de thèse : elle réveille Codex avec le
trigger dans le contexte, mais ne soumet pas de sortie automatiquement. En paper,
tout est simulé localement ; en IB plus tard, ce contrat se mappe vers
bracket/OCA/trailing quand le broker le permet.

> ~~### 4.3 bis Martingale bornée~~ — **retirée (2026-06-06).** Une martingale
> amplifie un système perdant ; tant que les KPI ne prouvent pas un edge stable,
> elle n'a pas sa place. Module, câblage daemon, contrat de sortie et tests
> supprimés. À reconsidérer seulement avec un edge démontré.

### 4.3 ter Sessions & gestion du contexte
Décisions d'archi (2026-06-06) sur « comment l'agent se réveille » :

- **Sessions jetables, pas de réutilisation.** Chaque appel passe par
  `acpx exec` (session temporaire, sans état partagé). Propriété visée :
  **isolation/idempotence** — aucun appel ne contamine le suivant (ce n'est pas
  du déterminisme bit-à-bit, un LLM échantillonne). Réutiliser une session
  persistante entre réveils a été écarté : l'historique regonflerait à chaque
  réveil (coût croissant, pas de prompt caching confirmé pour Codex), une session
  corrompue polluerait tous les réveils suivants, et ça créerait une 2ᵉ source de
  mémoire en concurrence avec les fichiers du repo.
- **L'état évolutif est externalisé en deux flux séparés**, repassés dans le
  contexte à chaque réveil : `memory.md` (boucle 1, humain, stratégie curée) et
  `state/learnings.jsonl` (boucle 2, machine, borné). L'agent émet un champ
  `learning` ; le daemon l'append et réinjecte les N derniers via
  `context.learnings`. La boucle 1 distille périodiquement le JSONL vers
  `memory.md`. **C'est la boucle de feedback runtime** — sans elle, l'« état
  évolutif de l'agent » n'évoluerait jamais.
- **REQUEST_CONTEXT reste stateless mais cohérent.** Le 2ᵉ `exec` (décision
  finale après recherche d'indicateurs) n'a pas l'historique du 1ᵉ ; on lui
  repasse `context.prior_rationale` (la demande initiale) pour qu'il reprenne son
  fil au lieu de raisonner à zéro.
- **Batch stateless (validé par probe, à implémenter).** Au lieu d'un `exec` par
  symbole dû (qui renvoie mandat+mémoire+cockpit N×), un seul `exec` pour tous
  les symboles dus → array de décisions. Probe 2026-06-06 : Spark rend 21/21
  décisions structurées propres en ~18s, stable sur 3 runs. Exigence
  d'implémentation : **isolation per-élément** (un élément JSON invalide → HOLD
  ce symbole, les autres passent) pour ne pas régresser sur le fail-isolation.

### 4.4 Risk gate (`trader/risk.py`) — codé en dur, NON négociable
**Fusible de sécurité**, pas un bridage de stratégie. Empêche un *bug* de l'agent
(boucle d'ordres, position absurde) de tout casser. Bornes externes :
position max, exposition max, perte max, débit d'ordres max. L'agent peut définir
ses propres règles plus fines par-dessus ; le gate est la borne ultime.

### 4.5 Labo backtest (`backtest/`) — maison
Moteur de backtest **maison** (data + engine + metrics + CLI), banc d'essai de la
boucle 1 : `backtest(strategy) → métriques`. Pas dans le chemin critique du
daemon. Léger, sans Docker ni dépendance payante.

> LEAN (Docker + `lean` CLI) était le plan initial, **abandonné** car l'offre
> backtest est devenue payante (2026-06-06). Réécriture maison à la place.

### 4.6 Mandat & mémoire (`mandate/`)
- `mandate.md` — objectif + marchés autorisés (édité en boucle 1)
- `memory.md` — l'agent y écrit sa stratégie évolutive et ses learnings

## 5. Univers (v1)

Agnostique à l'actif ; l'univers est une **config** (`config/`), pas du code.
v1 sur instruments disponibles via **yfinance** (data gratuite, démarrage
immédiat), couvrant les thèmes voulus via ETF quand nécessaire, actions liquides,
CAC 40, futures continus et forex spot proxy :

| Thème | Instrument v1 |
|---|---|
| S&P / Nasdaq / Dow | SPY / QQQ / DIA |
| Taïwan | EWT |
| France | EWQ / ^FCHI |
| Défense | ITA |
| Énergie | XLE |
| Pétrole / gaz | CL=F / BZ=F / NG=F |
| Métaux précieux | GC=F (or) |
| Crypto (24/7) | BTC-USD |
| Nasdaq individuelles | NVDA, AAPL… |
| Forex majors | EURUSD=X, GBPUSD=X, USDJPY=X, USDCHF=X, USDCAD=X, AUDUSD=X, NZDUSD=X, EURJPY=X |

> Crypto trade 24/7 et l'or (future COMEX) ~24/5 : avec le garde de fraîcheur
> (§4.3 ter), ils sont tradables hors des heures actions/FX — utile pour ne pas
> rester inactif tout le week-end.

Expansion vers les cotations natives (Euronext Paris, Taïwan, FX/futures broker)
= lignes ajoutées à l'univers une fois IB Gateway branchée.

## 6. Flux runtime

```
daemon réveille les symboles dus (timer global par défaut + overrides symbole)
  → tools/market: lit le contexte marché de l'univers
  → tools/portfolio: positions / PnL / KPI
  → codex_client: appel Codex → décision JSON structurée
  → exit_engine: applique les plans de sortie ouverts
  → risk.py: valide la nouvelle décision (fusible)
  → tools/execution: ordre simulé paper + log structuré
  → trade_plan: persiste le plan de sortie d'une ouverture
  → learnings: si l'agent a émis un `learning`, append dans state/learnings.jsonl
  → tools/scheduler: avance le timer global ou l'override du symbole
```

## 7. Garde-fous (déterministes, hors LLM)

- Codex injoignable / JSON invalide / erreur → **HOLD par défaut** (jamais de trade sur erreur).
- Risk gate rejette tout ordre hors bornes → HOLD + log.
- **`dry_run` par défaut** : log les ordres voulus sans les soumettre (premiers runs).
- **Kill switch** : un flag/fichier lu par le daemon ; si actif, zéro ordre.

## 8. Tests

- `codex_client` testé seul : input figé → validation du schéma de décision.
- `risk.py` unit-testé : cas limites (oversize, perte max, débit max).
- `execution.py` (sim) testé : fills déterministes.
- Le comportement de l'agent (stratégie) n'est PAS testé ici — il est défini en
  boucle 1, plus tard.

## 9. Périmètre & arrêt

Ce que je livre, **puis je m'arrête** :
- Skeleton du repo + arborescence
- Boîte à outils (`market`, `execution` sim, `portfolio`, `scheduler`, `memory`)
- `codex_client` (appel Codex programmatique)
- `risk.py` (fusible)
- `mandate.md` + `memory.md` (gabarits)
- `run.sh` (lance le daemon, `dry_run` par défaut)
- `README.md`
- Backtest maison dans `backtest/` (labo de la boucle 1)

**Hors périmètre (boucle 1, avec l'agent ensuite)** : la stratégie réelle, les
indicateurs, le calendrier de réveil, le prompt fin de Codex, le branchement IB.

## 10. Arborescence

```
casys-trader/
  trader/
    daemon.py          # boucle runtime
    codex_client.py    # appel Codex programmatique
    risk.py            # fusible (codé en dur)
    tools/
      market.py  execution.py  portfolio.py  scheduler.py  memory.py
  lab/                 # LEAN (labo backtest)
    lean.json  algos/
  mandate/
    mandate.md  memory.md
  skills/              # skills dispo (dev + runtime)
  config/
    universe.yaml  risk.yaml
  tests/
  run.sh
  README.md
```

## 11. Langage & outils

- **Python** (habitude uv).
- `codex` + `acpx` (déjà installés sur l'hôte).
- Backtest **maison** (`backtest/`) — pas de Docker/dotnet/LEAN.
