# Référence — Cockpit (TUI)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/interfaces/cockpit/` (app, shell, pages/, format, derive, modals, first_run, events, supervisor) · `trader/interfaces/ui/palette` · `trader/reporting/read_models/runtime_state`
> **Lancer** : `make watch` · **Rôle** : dashboard Textual « Decision Journal » + supervision du daemon.

Le cockpit est un **observateur** : il lit l'état (`state/`), l'affiche, et pilote
le cycle de vie du daemon. **Il ne participe JAMAIS aux décisions.** Ses seuls
write paths sont explicites et hors décision : le bloc `overrides:` de
`config/universe.yaml` (page Universe, pin/ban) et les écritures `w` de la page
Settings vers `config/*.yaml` — jamais `state/`, jamais le mandat, jamais
`config/risk.yaml`.

## Shell

```
Screen = Horizontal( NavRail 17 cols │ Vertical( KpiBand · page active · Footer ) )
```

- **NavRail** (`shell.py`) : brand, nav `1-8` (item actif accent, badge `▲N`
  sur *health* = anomalies), vitals (running/pid/mode/kill/session/horloge).
- **KpiBand** : EQUITY · CASH · UNREALIZED · CYCLE · NEXT WAKE · LLM — même
  bande sur toutes les pages.
- **Footer contextuel** : nav globale · touches de la page · `? help · q quit`.
  L'overlay `?` liste tous les raccourcis.

Thème unique `casys` (dark-amber) : accent structurel `#FFB86F` seul ;
vert/rouge réservés au P&L, au side (L/S) et à buy/sell ; `#e5c07b` = attention.
Palette Rich : `PALETTE_CASYS` + constantes `CASYS_*` (`interfaces/ui/palette.py`).

## Pages (1 module = 1 page, `pages/`)

| # | Page | Contenu | Touches locales |
|---|---|---|---|
| 1 | home | JOURNAL (raisonnement de l'agent : rationale, chip action, meter confiance, effet) · EQUITY · POSITIONS · NEXT TO FIRE | `enter` inspect |
| 2 | portfolio | table positions complète (side, P&L, stop, fraîcheur data) · EXPOSURE · FX → USD · CLOSED TRADES | `o` tri |
| 3 | decisions | ledger 24h (chips de filtre, expansion : rationale + audit tool_calls ✓/✗) · MIX · RISK GATE · MODEL | `b/s/h` filtre |
| 4 | plans | ARMED (ordres armés) · EXIT PLANS (stop/TP/protect/reviewed) · WATCHES (barres TTL) | |
| 5 | health | fraîcheur par venue · FX · sources · LLM · learnings · univers — cible du badge `▲N` | |
| 6 | logs | EVENTS (`events.jsonl`, chips par classe, cycles masqués par défaut) · AGENT TRACE | `c f F /` |
| 7 | universe | table par venue (hot/pool/⚚ pinned/✕ banned, décision, wake, data) · ROTATION · HOT-SET · OVERRIDES | `p b u` |
| 8 | settings | panels par fichier yaml, labels d'effet (applies now / next cycle / next rotation / restart required / locked), écriture atomique explicite | `enter w r` |

Contrat d'une page : widget avec `update_state(state: dict) -> None` (pull pur,
jamais d'exception) + **builders purs** `(state, now) → renderable` testables
sans UI (`format.py` pour le formatage, `derive.py` pour les dérivés d'état).

Drill-down symbole : `enter` sur toute table → `SymbolDetailScreen`
(`pages/symbol_detail.py`), `esc` ferme.

## Write paths de la page Universe

`p` pin / `b` ban / `u` undo écrivent le bloc `overrides:` de
`config/universe.yaml` via `trader/market/rotation/user_overrides.py`
(atomique, exclusion mutuelle pin/ban). La rotation applique les overrides à
chaque composition d'univers et **préserve le bloc** quand elle réécrit
`symbols:`. Un ban n'éjecte jamais un symbole sticky : la position reste gérée,
le symbole quitte seulement la sélection (confirmation demandée).

## Premier lancement

`daemon_vital_state = never_started` + aucun état → écran **preflight**
(`first_run.py`) : checks réels (config, mandate, state, IB Gateway), cartes
sécurité (dry-run par défaut, kill switch, risk gate), `s` pour démarrer.
Rien ne démarre sans `s`.

## Read model — `reporting/read_models/runtime_state`

Assemble l'état pour l'UI par **lectures tolérantes** (jamais de `raise`) des
fichiers `state/` : `current_report.json`/`last_report.json`,
`daemon_status.json`, `history.jsonl`, `decisions.jsonl`, `scheduler.json`
(watches + `default_next_wake` + `symbol_wakes`), `trade_plans.json`,
`venue_state.json`. Les events `events.jsonl` sont lus séparément par
`interfaces/cockpit/events.py` (glyphes : `·` hold, `▲/▼` fills, `✗` risk,
`⚑` watch, `◇` learning, `▶/■` cycles).

## Supervision du daemon

Via `interfaces/cockpit/supervisor` : `s` lancer · `x` arrêter (SIGINT,
confirmation) · `k` kill-switch (confirmation) · `q` quitter (confirmation si
le daemon est vivant). Détail dans [run-the-daemon](../how-to/run-the-daemon.md).

## Captures

`uv run python scripts/cockpit_screenshots.py state/screenshots` — 8 pages +
overlay d'aide, SVG + PNG.

## Voir aussi
- [How-to : lancer le daemon](../how-to/run-the-daemon.md) · [reporting](reporting.md).
