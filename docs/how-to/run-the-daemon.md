# How-to — Lancer / relancer / arrêter le daemon

> **Type** : How-to (Diátaxis) — procédure orientée tâche.
> **Superviseur** : `trader/interfaces/cockpit/supervisor.py` · **State** : `state/daemon.pid`, `state/daemon_status.json`, `state/daemon_console.log`

Lorsqu'il est lancé par le superviseur, le daemon devient un **process détaché**
qui survit au cockpit. Le superviseur gère lock, anti-doublon, rotation du log
et détachement. `make live` reste volontairement au premier plan pour le dev.

## Démarrer

| Objectif | Commande |
|---|---|
| Daemon en paper (foreground, dev) | `make live` |
| Cockpit (dashboard + supervision + pilotage) | `make watch` |
| Daemon détaché **+** logs Gonzo | `make live-logs` |
| Un seul cycle dry-run (test) | `make once` |

`make live-logs` lance le daemon via le superviseur (**anti-doublon** : si un
daemon tourne déjà → « already_running », pas de second). Quitter Gonzo (`q`) ne
tue PAS le daemon.

## Arrêter (proprement)

Le superviseur envoie **SIGINT** (jamais SIGKILL) après vérification d'identité :

```bash
uv run python -c 'from pathlib import Path; from trader.interfaces.cockpit.supervisor import stop_daemon; print(stop_daemon(pid_file=Path("state/daemon.pid"), status_file=Path("state/daemon_status.json")))'
```

Un résultat `stopped=True, reason='sigint_sent'` confirme l'envoi du signal,
pas encore la fin effective du processus.

Ou depuis le cockpit (`make watch`) : `x` arrête le daemon avec confirmation ;
`q` propose soit de quitter seulement le cockpit, soit d'arrêter puis quitter.

## Arrêt pendant une décision

Le chemin queue courant arrête proprement ses pools et conserve les tâches
durables ; un travail non terminé sera repris selon son bail et son backoff. Le
mode batch legacy peut encore attendre ses appels LLM en vol jusqu'au timeout.
Dans les deux cas, utiliser le superviseur puis attendre que son état vital ne
soit plus `alive`, au lieu de tuer le PID :

```bash
uv run python - <<'PY'
from pathlib import Path
from time import monotonic, sleep

from trader.interfaces.cockpit.supervisor import daemon_vital_state, stop_daemon

pid_file = Path("state/daemon.pid")
status_file = Path("state/daemon_status.json")
result = stop_daemon(pid_file=pid_file, status_file=status_file)
print(result)
if not result.stopped and result.reason not in {"no_daemon", "pid_dead"}:
    raise SystemExit(f"arrêt refusé: {result.reason}")

deadline = monotonic() + 300
while daemon_vital_state(status_file).status == "alive" and monotonic() < deadline:
    sleep(0.5)
vital = daemon_vital_state(status_file)
print(vital)
if vital.status == "alive":
    raise SystemExit("daemon encore vivant après 300 s; ne pas relancer")
PY
```

Ne lancer le nouveau daemon qu'après cette confirmation. Le superviseur vérifie
l'identité du process et envoie SIGINT, jamais SIGKILL.

## Relancer le daemon

Le daemon lit le code + le `.env` **au démarrage** (cf. `CASYS_DECISION_BATCH_PARALLELISM`,
`CASYS_LOG_LEVEL`). Après le stop vérifié ci-dessus, relancer via le superviseur :

```bash
make live-logs
```

`launch_daemon` **rotate le log** au-delà de `MAX_LOG_SIZE_BYTES` (5 Mo) et refuse
un second daemon (`already_running`). Pour un **log vierge** : archiver
`daemon_console.log` (mv) pendant que le daemon est arrêté avant `launch`.

## Vérifier que ça tourne

```bash
pid=$(tr -d '[:space:]' < state/daemon.pid)
ps -o pid,ppid,etime,stat,command -p "$pid"                 # process vivant ?
uv run casys-trader status --json                           # phase et état runtime
grep '\[config\]' state/daemon_console.log | tail -1        # config au démarrage
```

`daemon_vital_state(status_file)` (superviseur) donne l'état vital (pid + identité).
`code_version` prouve le code servi, pas le modèle : vérifier les modèles demandés
avec `make models`, puis le provider/modèle réellement persisté dans les derniers
runs ou décisions.

## Voir aussi
- [Lire les logs](read-logs.md) · [Mesurer / replay](measure-and-replay.md)
- [Changer les modèles](manage-model-presets.md) ·
  [Diagnostiquer les rapports](refresh-and-diagnose-reports.md)
- Architecture §8 (état persistant), §9 (LLM/acpx).
