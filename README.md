# casys-trader

Agent de trading **autonome** en paper trading. Le brain décideur est **Codex**,
appelé programmatiquement. **La stratégie, les indicateurs et le calendrier de
réveil ne sont pas codés** : l'agent les définit lui-même via le mandat et la
mémoire.

## Les deux boucles

- **Boucle 1 — Dev/design** : Erwan + Claude, en conversation dans le repo. On
  fait évoluer le mandat, les outils, le comportement, les marchés. Le backtest
  maison (`backtest/`) rejoue l'agent sur l'historique pour itérer.
- **Boucle 2 — Runtime** : le daemon (`trader/runtime/daemon.py`), lancé par `run.sh`.
  Réveil des symboles dus → contexte → Codex → risk gate → exécution paper → log
  → prochain réveil global, override par symbole ou veille indicateur temporaire.

Le repo est l'interface partagée : `mandate/`, `config/`, `mandate/memory.md` sont
édités en boucle 1 et lus par le daemon en boucle 2.

## Démarrage

```bash
uv sync
./run.sh --once          # un cycle, DRY-RUN (aucun ordre exécuté)
./run.sh                 # boucle continue, dry-run
./run.sh --live --once   # exécute réellement en paper (SimBroker)
```

Le daemon runtime lit ses barres uniquement via Interactive Brokers. IB Gateway
ou TWS doit être lancé et l'API doit accepter les connexions, sinon le cycle est
sauté puis retenté au réveil suivant. Connexion par défaut : `127.0.0.1:4002`,
`clientId=17`.

```bash
CASYS_IB_HOST=127.0.0.1 CASYS_IB_PORT=4002 CASYS_IB_CLIENT_ID=17 ./run.sh --once
./run.sh --once --ib-host 127.0.0.1 --ib-port 4002 --ib-client-id 17
```

## Sécurité (safe defaults)

- **Dry-run par défaut** : `--live` requis pour exécuter.
- **Kill switch** : `touch KILL` à la racine → plus aucun ordre.
- **Risk gate** (`trader/execution/risk.py` + `config/risk.yaml`) : fusible non négociable.
- **Fail-safe Codex** : toute erreur (timeout, JSON invalide, binaire absent) → HOLD.

## Structure

```
trader/
  runtime/           daemon, CLI, logging, version, IB attach
  agent/             contexte agent, client Codex, transport LLM/acpx
  planning/          plans, veilles indicateurs, exit engine, relevance gate
  execution/         RiskGate et contraintes d'ordre
  application/       services du cycle runtime extraits du daemon
  reporting/         ledger, attribution, stats, audit décisionnel
  market/            indicateurs, FX, macro, radar, régime
  tools/
    ib_source.py     données marché runtime (Interactive Brokers)
    market.py        données marché yfinance (backtest/cache, hors daemon)
    execution.py     ordres (SimBroker paper -> IB plus tard, même interface)
    portfolio.py     positions / PnL / KPI
    scheduler.py     cadence globale par défaut + overrides par symbole
    memory.py        stratégie + learnings persistants
backtest/                            <- backtest maison (SimBroker + yfinance)
config/  universe.yaml  risk.yaml
mandate/ mandate.md  memory.md      <- définis en boucle 1
skills/  skills dispo (dev + runtime)
docs/specs/                          <- design de référence
```

## Backtest maison (`backtest/`)

Rejeu de l'agent sur l'historique (SimBroker + yfinance), **échantillonné** pour
borner le coût (un appel Codex par pas × symbole).

```bash
# Plumbing sans Codex (déterministe, gratuit)
uv run python -m backtest --mock --days 30 --sample-every 5

# Vrai backtest Codex, borné (1 symbole, 1 pas = 1 appel)
uv run python -m backtest --days 20 --sample-every 5 --max-steps 1 --symbols SPY

# Ancien mode brut sans frais, utile pour isoler la stratégie du coût broker
uv run python -m backtest --mock --days 30 --commission-model none
```

⚠️ Le backtest **ne mesure pas** la vraie perf : fills parfaits (SimBroker, pas de
slippage ; frais IBKR estimés par défaut), data leakage possible (le LLM a pu voir
l'historique), rejeu échantillonné. Pour un agent adaptatif, le **forward paper**
reste l'éval de référence. Le rapport complet est écrit dans
`state/last_backtest.json`.

Le daemon live utilise aussi `TRADER_COMMISSION_MODEL=ibkr` par défaut pour le
paper. Chaque fill persiste `commission`, `commission_currency` et
`commission_model`; `TRADER_COMMISSION_MODEL=none` permet de revenir à l'ancien
mode brut.

## Univers courant

L'univers runtime est dans `config/universe.yaml`. Le daemon résout ses barres
via IB (`trader/tools/ib_source.py`) et le mapping broker natif
`config/ib_contracts.yaml`. Les tickers Yahoo restent utiles pour le backtest et
le cache via `trader/tools/market.py`, mais ne sont plus appelés par le daemon.

## Appel LLM

Le brain décideur passe par une API de transport agnostique. Primaire :
**`acpx --format quiet exec`** (codex = agent par défaut d'acpx), modèle
**`gpt-5.5/medium`**. Fallback optionnel : endpoint
OpenAI-compatible Ollama Cloud, configuré dans `.env`.
Les nouvelles décisions logguent donc `llm_provider=acpx` et
`llm_model=gpt-5.5/medium`.

```bash
TRADER_OLLAMA_API_KEY=...
TRADER_OLLAMA_BASE_URL=https://ollama.com/v1
TRADER_OLLAMA_MODEL=nemotron-3-nano:30b-cloud
```

Le fallback ne sert que sur erreurs retryables fournisseur/quota/rate-limit/
timeout. Un JSON invalide reste un `HOLD` avec erreur, pour éviter de changer de
cerveau parce que le contrat de sortie a été cassé. Chaque décision garde
`llm_provider`, `llm_model` et `llm_fallback_reason`; les fills live alimentent
`state/model_performance.jsonl` pour comparer les modèles dans le temps.

Le consolidateur de learnings utilise une config séparée du brain décideur :
seuil `50`, agent `codex`, modèle `gpt-5.5/high`. Overrides :
`--learning-consolidation-threshold`, `--consolidator-acpx-bin`,
`--consolidator-acpx-agent`, `--consolidator-model`,
`--consolidator-timeout-s`, ou les env
`TRADER_LEARNING_CONSOLIDATION_THRESHOLD`, `TRADER_CONSOLIDATOR_ACPX_BIN`,
`TRADER_CONSOLIDATOR_ACPX_AGENT`, `TRADER_CONSOLIDATOR_MODEL`,
`TRADER_CONSOLIDATOR_TIMEOUT_S`.
Il peut utiliser un fallback Ollama Cloud dédié au consolidateur avant de
retomber sur les variables Ollama globales :

```bash
TRADER_CONSOLIDATOR_OLLAMA_API_KEY=...      # optionnel si TRADER_OLLAMA_API_KEY existe
TRADER_CONSOLIDATOR_OLLAMA_BASE_URL=https://ollama.com/v1
TRADER_CONSOLIDATOR_OLLAMA_MODEL=glm-5.1:cloud
```

Les erreurs ACPX retryables du consolidateur, y compris `Internal error` quand
le fournisseur primaire n'a plus de crédit, passent sur ce fallback sans muter
le store consolidé tant qu'aucun JSON valide n'est reçu.

Décision pure : `--allowed-tools ""` + `--no-terminal` côté acpx (aucun outil, le
brain ne fait que raisonner sur le contexte fourni). `exec` = session jetable
→ isolation/idempotence (aucun appel ne contamine le suivant) ; l'état évolutif de
l'agent est externalisé — `mandate/memory.md` (boucle 1) et
`state/learnings.jsonl` (boucle 2, machine) — et repassé dans le prompt. Contrat
validé de bout en bout (JSON → `Decision`, incluant un `next_wake_in_minutes`
optionnel par symbole, une `indicator_watch` temporaire, et un `learning`
optionnel réinjecté au réveil suivant).

## Semantic layer / indicateurs

Le LLM ne calcule pas les indicateurs "de tête". Le code expose une semantic
layer locale (pattern GeoNexus, sans MCP) : catalogue gouverné + calculs
déterministes + CLI JSON. Le prompt runtime reste compact : le daemon envoie un
`context.cockpit` sous forme `cols` + `rows` (`r`, `vol`, `z`, `er`, `ac`, `rs`,
`sz`) et pas les barres brutes. Si le modèle veut creuser, il renvoie
`REQUEST_CONTEXT`; le daemon calcule les indicateurs bornés localement puis
ré-appelle Codex une seule fois pour la décision finale.

Le catalogue inclut aussi des signaux de chandeliers japonais et d'analyse
chartiste sous forme numérique compacte : `candlestick_signal`,
`candle_body_ratio`, `candle_wick_skew`, `chart_breakout`, `trend_slope`,
`range_position`.

L'axe temporel est explicite : les requêtes suivent le cube
`symbol × indicator × timeframe × lookback × window × as_of`. Timeframes
gouvernés : `15m`, `30m`, `1h`, `4h`, `1d`. En runtime IB, le `4h` est demandé
nativement (`4 hours`) ; les agrégations internes du cockpit restent inchangées.

```bash
casys-trader semantic describe --json
casys-trader indicators list --json
casys-trader indicators get --symbol SPY --timeframe 4h --lookback 1mo --window 48 --names efficiency_ratio,z_score,volatility --json
casys-trader indicators compare --family energy --timeframe 1h --metrics return,relative_strength,volatility --json
```

Le daemon importe les mêmes fonctions Python et expose seulement
`context.semantic.requestable_indicator_ids` dans le prompt initial.

## Réveils par timer ou indicateurs

À chaque décision, l'agent peut choisir un simple `next_wake_in_minutes` ou poser
une `indicator_watch`. Une watch contient une combinaison `all|any` de conditions
sur indicateurs déterministes, chacune avec son `symbol`, `indicator`, `op`,
`value`, `interval` (`15m`, `30m`, `1h`, `4h`, `1d`), `lookback`, `window` et
`as_of`. Elle a un `ttl_minutes` : après expiration, elle est purgée et le symbole
retombe sur son timer normal.

Le daemon scanne ces watches au poll sans appeler Codex. Si une combinaison
déclenche, la watch est retirée, le symbole devient dû immédiatement, et le
trigger est injecté dans `context.indicator_triggers` pour la décision suivante.
`on_trigger=WAKE_WITH_ORDER_INTENT` signifie que le trigger transporte une
intention d'ordre structurée, mais l'ordre repasse par la boucle Codex/risk gate
au réveil plutôt que d'être soumis directement hors contexte.

## Observabilité live

Pendant `casys-trader --live`, la console loggue les étapes importantes :
chargement marché, symbole courant (`[decision 4/21]`), appels modèle, demandes
de contexte, décisions, blocages risk gate, ordres et sommeil scheduler. Les
mêmes informations sont persistées en cours de cycle :

```bash
casys-trader status
casys-trader status --json
```

Fichiers utiles : `state/daemon_status.json`, `state/current_report.json`,
`state/events.jsonl`, `state/decisions.jsonl`, puis `state/last_report.json` en
fin de cycle.

`state/decisions.jsonl` est le ledger append-only des décisions agent. Il garde
la décision brute, le prix, le snapshot marché/portfolio minimal, le résultat
runtime (`executed`, `reason`), la version git qui a produit la décision, et un
champ `labels` vide pour les verdicts ex-post futurs (`good` / `bad` /
`neutral` par horizon). L'audit écrit aussi sa propre version git et regroupe
les verdicts + pourcentages par commit de décision (`summary_by_commit`,
`metrics_by_commit`) pour comparer les taux avant/après une mise à jour. Pour
récupérer les décisions déjà présentes dans les rapports live :

```bash
casys-trader decisions seed-existing
casys-trader decisions seed-events --include-archives
casys-trader decisions backfill-code-version
casys-trader decisions list --json
casys-trader decisions audit --horizons 1h,4h,1d --threshold-pct 0.5
casys-trader decisions stats --horizon 1h --min-known 10
casys-trader decisions bench --horizon 4h --limit 10
casys-trader decisions bench --horizon 4h --limit 10 --verdicts missed,bad --hide-original --reconstruct-context
```

`decisions bench` demande un avis contrefactuel à un petit banc de modèles sur
des décisions déjà auditées, sans exposer le rendement futur dans le prompt. Il
score ensuite localement les actions proposées (`BUY` / `SELL` / `HOLD`) contre
l'audit forward et écrit `state/last_decision_bench.json`. Par défaut, le banc
compare `gpt-5.3-codex-spark/medium`, `gpt-5.3-codex-spark/xhigh`,
`gpt-5.5/medium`, `gpt-5.5/xhigh`, `nemotron-3-super:cloud` et
`glm-5.1:cloud`. Utiliser `--dry-run --json` pour inspecter les cas et le
prompt avant de consommer des appels modèle. `--reconstruct-context` ajoute un
contexte as-of reconstruit depuis le cache historique (`state/backtest_cache`) :
cockpit, prix as-of et régime par famille, sans raw bars ni futur. Ce mode n'est
pas un replay exact des vieux prompts si le snapshot complet n'avait pas été
persisté à l'époque ; le rapport le marque donc `exact_replay=false`.

Le lancement live est incremental : si `state/scheduler.json` contient déjà des
timers futurs, le daemon ne réanalyse pas tout l'univers au démarrage. Il ne
traite que les symboles dus. Pour forcer un scan complet malgré les timers :

```bash
casys-trader daemon --live --bootstrap-all
```

## Plans de sortie

Une entrée peut inclure un `exit_plan`. Codex définit la thèse et les seuils ;
le daemon applique ensuite le plan de façon déterministe, sans attendre un nouveau
raisonnement LLM :

- `hard_stop` : stop dur.
- `take_profits` : TP1/TP2/… avec fraction de position.
- `after_fill` : action après TP, par exemple `move_stop_to_breakeven`.
- `trailing_stop` : stop suiveur activable dès l'entrée ou après un TP.
- `profit_protection` : sécurisation progressive optionnelle demandée par
  l'agent, par exemple armement à `+0.5R`, déclenchement après giveback,
  fermeture partielle et stop du reste à break-even. Le daemon ne l'ajoute pas
  automatiquement.
- `exit_watch` : veille indicateur attachée au trade. Elle réveille l'agent si
  des signaux invalident la thèse (`trend_slope`, `relative_strength`,
  chandeliers, etc.), mais ne ferme pas automatiquement la position.
- `max_hold_minutes` : sortie temps pour les scalps morts.

Les plans ouverts sont persistés dans `state/trade_plans.json`. En paper trading,
le moteur simule bracket/OCA/trailing localement ; plus tard, l'adapter IB pourra
mapper ces plans vers bracket/OCA/trailing natifs quand disponible.

## État actuel

Infrastructure posée et testée (fusible, SimBroker, câblage du cycle, appel Codex
réel, scheduler global + overrides symbole, semantic layer d'indicateurs). Backtest
maison (`backtest/`) en cours de construction (SimBroker + yfinance, rejeu
échantillonné pour limiter les appels Codex). LEAN abandonné : son CLI/API local
est payant (84 $/mois) et son backtest dense ne convient pas à un agent
LLM-in-the-loop. **Prochaine étape (boucle 1)** : enrichir le catalogue
d'indicateurs et durcir l'exploitation paper IB.
