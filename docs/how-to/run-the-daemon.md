# How-to — Lancer / relancer / arrêter le daemon

> **Type** : How-to (Diátaxis) — procédure orientée tâche.
> **Superviseur** : `trader/cockpit/supervisor.py` · **State** : `state/daemon.pid`, `state/daemon_status.json`, `state/daemon_console.log`

Le daemon est un **process indépendant** (PPID=1) qui survit au cockpit. Le
superviseur gère lock, anti-doublon, rotation du log, détachement.

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

```python
from pathlib import Path
from trader.cockpit.supervisor import stop_daemon
stop_daemon(pid_file=Path("state/daemon.pid"), status_file=Path("state/daemon_status.json"))
```

Ou depuis le cockpit (`make watch`) : touche d'arrêt / quit avec confirmation.

## ⚠️ Piège : SIGINT pendant un batch de décision

Si le daemon est en phase `deciding_batch` (`ThreadPoolExecutor` + `proc.communicate`),
**le SIGINT ne l'interrompt PAS** — `shutdown(wait=True)` attend la fin des appels
LLM en vol (jusqu'à `decision_timeout_s`, défaut 900 s). Le daemon meurt en ~1-2 s
seulement quand il est **hors batch** (pause inter-cycle, phase ≠ `deciding_batch`).

**Restart robuste** : envoyer SIGINT, puis boucler tant que vivant en re-signalant
dès que `phase != "deciding_batch"` (le main thread est alors en `sleep`,
immédiatement interruptible) :

```python
import os, time, signal, json
old = int(open("state/daemon.pid").read())
alive = lambda p: (os.kill(p, 0) or True) if _try(p) else False  # os.kill(p,0) → OSError si mort
# stop_daemon(...) puis :
for _ in range(240):
    if not _alive(old): break
    if json.load(open("state/daemon_status.json"))["phase"] != "deciding_batch":
        os.kill(old, signal.SIGINT)
    time.sleep(1)
# puis launch_daemon(...)
```

## Relancer (appliquer un changement de code / .env)

Le daemon lit le code + le `.env` **au démarrage** (cf. `CASYS_DECISION_BATCH_PARALLELISM`,
`CASYS_LOG_LEVEL`). Pour appliquer un changement : **stop → launch** via le superviseur.

```python
from pathlib import Path
from trader.cockpit.supervisor import launch_daemon
launch_daemon(pid_file=Path("state/daemon.pid"), log_file=Path("state/daemon_console.log"),
              root=Path("."), status_file=Path("state/daemon_status.json"))
```

`launch_daemon` **rotate le log** au-delà de `MAX_LOG_SIZE_BYTES` (5 Mo) et refuse
un second daemon (`already_running`). Pour un **log vierge** : archiver
`daemon_console.log` (mv) pendant que le daemon est arrêté avant `launch`.

## Vérifier que ça tourne

```bash
cat state/daemon.pid | xargs ps -o pid,etime,stat -p        # process vivant ?
grep '\[config\]' state/daemon_console.log | tail -1        # config au démarrage (parallelism, etc.)
```

`daemon_vital_state(status_file)` (superviseur) donne l'état vital (pid + identité).

## Voir aussi
- [Lire les logs](read-logs.md) · [Mesurer / replay](measure-and-replay.md)
- Architecture §8 (état persistant), §9 (LLM/acpx).
