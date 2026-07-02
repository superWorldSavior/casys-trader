# Référence — Cockpit (TUI)

> **Type** : Reference (Diátaxis).
> **Code** : `trader/cockpit/` (app, supervisor, events) · `trader/ui/` (rich_panels, palette, tui) · `trader/read_models/runtime_state`
> **Lancer** : `make watch` · **Rôle** : dashboard Textual + supervision du daemon.

Le cockpit est un **observateur** : il lit l'état (`state/`), l'affiche, et pilote
le cycle de vie du daemon. **Il ne participe JAMAIS aux décisions** (read-only sur
la donnée live).

## Ce qu'il affiche

Le **Status** est un widget Textual `CockpitStatus` (`cockpit/app.py`) ; les autres
panneaux sont des **builders purs** dans `ui/rich_panels.py` :

| Panneau | Contenu |
|---|---|
| Status | Équité $, cash, P&L, phase daemon, horloge UTC, kill-switch |
| Positions + P&L net | positions ouvertes, P&L net (frais inclus) |
| Plans de sortie | exit plans enrichis (hard_stop/TP résolus) |
| Plans armés | veilles `EXECUTE_ORDER` en attente |
| Veilles | `indicator_watch` (WAKE) actives |
| Courbe d'équité | historique (`history.jsonl`) |
| KPI compact | métriques live (cf. [reporting](reporting.md) `stats`) |
| Décisions | table des décisions récentes |
| Activité LLM | appels du cycle (`model_calls_used`/max), learnings bruts en attente, statut consolidation |
| Attribution | trades clôturés (round-trips, cf. [reporting](reporting.md)) |
| Derniers apprentissages | learnings récents (`_build_learnings_panel`) |
| Trades clôturés | round-trips avec P&L net (`build_closed_trades_table`) |
| Santé données | fraîcheur / sources / stale |
| Univers | symboles suivis par place |

## Read model — `read_models/runtime_state`

Assemble l'état pour l'UI par **lectures tolérantes** (jamais de `raise`) des
fichiers `state/` :

| Fichier | Usage |
|---|---|
| `current_report.json` / `last_report.json` | dernier cycle |
| `daemon_status.json` | phase, pid, progression |
| `history.jsonl` | courbe d'équité (points non nuls) |
| `decisions.jsonl` | décisions récentes |

(Les **events** `events.jsonl` sont lus séparément par `cockpit/events.py` dans la
couche UI, pas par le read model.)

`load_state(path)` → dict ou `None` (absent/illisible). Ne bloque jamais l'UI sur
un fichier corrompu.

## Supervision du daemon

Via `cockpit/supervisor` (touches du cockpit) : lancer / arrêter (SIGINT) /
kill-switch, `daemon_vital_state`. Détail dans [run-the-daemon](../how-to/run-the-daemon.md).

## Thème

`ui/palette` — couleurs par niveau/event. Le cockpit dessine ses propres couleurs
(indépendant du thème du terminal ; cf. how-to logs pour Gonzo, qui est séparé).

## Voir aussi
- [How-to : lancer le daemon](../how-to/run-the-daemon.md) · [reporting](reporting.md).
