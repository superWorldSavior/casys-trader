# casys-trader — Design

> Statut : design validé (brainstorming) — 2026-06-05
> Auteur : Erwan + Claude

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
marchés. On utilise **LEAN** comme labo de backtest pour éprouver une stratégie
sur l'historique avant de la déployer.

### Boucle 2 — Runtime (autonome, rapide)
Un **daemon** lancé par un script. Le brain Codex est appelé programmatiquement :
il se réveille quand il veut, lit le marché via ses outils, décide, passe l'ordre
(après le risk gate), écrit ses learnings en mémoire, planifie son prochain
réveil.

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
Boucle : `réveil → construit le contexte → appelle Codex → risk gate → exécute → log → planifie prochain réveil`.

- **Python pur**, pas de dépendance Docker pour trader.
- Data légère (yfinance pour la v1) derrière l'interface `market.py`.
- Exécution **simulée** (fill simulator paper) en v1, derrière `execution.py` —
  swappable vers IB (`ib_async` ou LEAN) plus tard **sans changer l'algo**.

### 4.2 Boîte à outils de l'agent (`trader/tools/`)
Primitives composables, contrats étroits :
- `market.py` — lire données marché (prix, barres, lookback configurable)
- `execution.py` — placer un ordre (sim paper now → IB après, **même interface**)
- `portfolio.py` — positions, cash, PnL, KPI de performance
- `scheduler.py` — l'agent fixe son prochain réveil (`schedule_next_wake`)
- `memory.py` — lire/écrire la stratégie et les learnings persistants

### 4.3 Decision brain (`trader/codex_client.py`)
Appel **programmatique** à Codex (via `codex exec` / acpx headless). Reçoit le
contexte JSON, force une **sortie structurée** (action, taille/poids, confidence,
rationale).

### 4.4 Risk gate (`trader/risk.py`) — codé en dur, NON négociable
**Fusible de sécurité**, pas un bridage de stratégie. Empêche un *bug* de l'agent
(boucle d'ordres, position absurde) de tout casser. Bornes externes :
position max, exposition max, perte max, débit d'ordres max. L'agent peut définir
ses propres règles plus fines par-dessus ; le gate est la borne ultime.

### 4.5 Labo backtest (`lab/`) — LEAN
**LEAN** (Docker + `lean` CLI) comme banc d'essai de la boucle 1. Outil que
l'agent pourra appeler : `backtest(strategy) → métriques`. Pas dans le chemin
critique du daemon. Futur backend d'exécution réelle possible.

### 4.6 Mandat & mémoire (`mandate/`)
- `mandate.md` — objectif + marchés autorisés (édité en boucle 1)
- `memory.md` — l'agent y écrit sa stratégie évolutive et ses learnings

## 5. Univers (v1)

Agnostique à l'actif ; l'univers est une **config** (`config/`), pas du code.
v1 sur instruments **listés US** (data gratuite, démarrage immédiat), couvrant les
thèmes voulus :

| Thème | Instrument US v1 |
|---|---|
| S&P / Nasdaq / Dow | SPY / QQQ / DIA |
| Taïwan | EWT |
| France | EWQ |
| Défense | ITA |
| Pétrole / gaz | XLE / USO / UNG |
| Nasdaq individuelles | NVDA, AAPL… |

Expansion vers les cotations natives (future CAC, Euronext Paris, Taïwan) = lignes
ajoutées à l'univers une fois IB Gateway branchée.

## 6. Flux runtime

```
daemon réveil (heure planifiée par l'agent)
  → tools/market: lit le contexte marché de l'univers
  → tools/portfolio: positions / PnL / KPI
  → codex_client: appel Codex → décision JSON structurée
  → risk.py: valide la décision (fusible)
  → tools/execution: ordre simulé paper + log structuré
  → tools/memory: écrit learnings
  → tools/scheduler: planifie le prochain réveil
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
- Installation/config de LEAN dans `lab/` (labo backtest)

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
- `docker` + `dotnet` présents → LEAN installable.
