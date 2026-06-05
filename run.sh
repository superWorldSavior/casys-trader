#!/usr/bin/env bash
# Lance le daemon casys-trader.
# Safe default : DRY-RUN (n'exécute aucun ordre, log seulement).
# Pour exécuter réellement en paper : ./run.sh --live
# Un seul cycle : ./run.sh --once
set -euo pipefail
cd "$(dirname "$0")"

# Kill switch : créer un fichier KILL à la racine pour stopper tout ordre.
exec uv run python -m trader.daemon "$@"
