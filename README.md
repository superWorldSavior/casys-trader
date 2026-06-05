# casys-trader

Agent de trading **autonome** en paper trading. Le brain décideur est **Codex**,
appelé programmatiquement. **La stratégie, les indicateurs et le calendrier de
réveil ne sont pas codés** : l'agent les définit lui-même via le mandat et la
mémoire.

## Les deux boucles

- **Boucle 1 — Dev/design** : Erwan + Claude, en conversation dans le repo. On
  fait évoluer le mandat, les outils, le comportement, les marchés. Le backtest
  maison (`backtest/`) rejoue l'agent sur l'historique pour itérer.
- **Boucle 2 — Runtime** : le daemon (`trader/daemon.py`), lancé par `run.sh`.
  Réveil → contexte → Codex → risk gate → exécution paper → log → prochain réveil.

Le repo est l'interface partagée : `mandate/`, `config/`, `mandate/memory.md` sont
édités en boucle 1 et lus par le daemon en boucle 2.

## Démarrage

```bash
uv sync
./run.sh --once          # un cycle, DRY-RUN (aucun ordre exécuté)
./run.sh                 # boucle continue, dry-run
./run.sh --live --once   # exécute réellement en paper (SimBroker)
```

## Sécurité (safe defaults)

- **Dry-run par défaut** : `--live` requis pour exécuter.
- **Kill switch** : `touch KILL` à la racine → plus aucun ordre.
- **Risk gate** (`trader/risk.py` + `config/risk.yaml`) : fusible non négociable.
- **Fail-safe Codex** : toute erreur (timeout, JSON invalide, binaire absent) → HOLD.

## Structure

```
trader/
  daemon.py          boucle runtime
  codex_client.py    appel Codex programmatique (sortie JSON validée)
  risk.py            le fusible
  tools/
    market.py        données marché (yfinance v1)
    execution.py     ordres (SimBroker paper -> IB plus tard, même interface)
    portfolio.py     positions / PnL / KPI
    scheduler.py     l'agent fixe son prochain réveil
    memory.py        stratégie + learnings persistants
backtest/                            <- backtest maison (SimBroker + yfinance)
config/  universe.yaml  risk.yaml
mandate/ mandate.md  memory.md      <- définis en boucle 1
skills/  skills dispo (dev + runtime)
docs/specs/                          <- design de référence
```

## Appel Codex

Le brain décideur est appelé via **`acpx --format quiet exec`** (codex = agent par
défaut d'acpx). Décision pure : `--allowed-tools ""` + `--no-terminal` (aucun outil,
le brain ne fait que raisonner sur le contexte fourni). `exec` = session temporaire
→ déterministe ; l'état évolutif de l'agent vit dans `mandate/memory.md`, passé dans
le prompt. Contrat de sortie validé de bout en bout (JSON → `Decision`).

## État actuel

Infrastructure posée et testée (fusible, SimBroker, câblage du cycle, appel Codex
réel). Backtest maison (`backtest/`) en cours de construction (SimBroker + yfinance,
rejeu échantillonné pour limiter les appels Codex). LEAN abandonné : son CLI/API
local est payant (84 $/mois) et son backtest dense ne convient pas à un agent
LLM-in-the-loop. **Prochaine étape (boucle 1)** : définir avec l'agent le mandat
réel, le comportement de Codex, la cadence de réveil — puis brancher IB
(`ib_async`) quand le compte paper est prêt.
