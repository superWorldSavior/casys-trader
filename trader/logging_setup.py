"""logging_setup — installe un handler adapté au contexte d'exécution.

TTY (terminal interactif) → RichHandler : couleurs par niveau, timestamps
courts, sans path/lineno.

Non-TTY (pipe, CI, redirect) → StreamHandler texte simple. Les messages
du daemon peuvent contenir du markup Rich ([green]...) mais ce module
s'assure que le formatter n'interprète PAS ce markup en mode non-TTY
(option markup=False sur le handler, ou handler sans console Rich).

Approche retenue : le markup Rich N'EST PAS injecté dans les chaînes de
message (les fichiers machine restent indemnes). La mise en couleur est
exclusivement pilotée par le RichHandler via les niveaux de log standard.
Cela garantit zéro fuite de balises dans les logs non-TTY.

Couverture des loggers : le handler est installé sur le logger "trader"
(ancêtre commun de tous les trader.*) ET sur "casys-trader". Les deux
loggers ont propagate=False pour ne pas doubler vers le root logger.
Les sous-loggers (trader.tools.data_source, etc.) propagent vers "trader"
par défaut (propagate=True) et sont donc couverts sans handler supplémentaire.
"""

from __future__ import annotations

import logging
import sys
from typing import IO


def setup_logging(
    level: int = logging.INFO,
    stream: IO | None = None,
) -> None:
    """Configure les loggers 'trader' et 'casys-trader'.

    Le handler est partagé et installé sur les deux loggers racines du projet.
    Tous les trader.* propagent vers 'trader' par défaut → couverts.

    Args:
        level: niveau de log (ex. logging.INFO).
        stream: flux de sortie. None → sys.stdout. Injecter un faux stream
                en test pour contrôler isatty().
    """
    if stream is None:
        stream = sys.stdout

    is_tty = callable(getattr(stream, "isatty", None)) and stream.isatty()

    if is_tty:
        from rich.console import Console
        from rich.logging import RichHandler

        console = Console(file=stream)  # type: ignore[arg-type]
        handler: logging.Handler = RichHandler(
            level=level,
            console=console,
            rich_tracebacks=False,
            show_path=False,
            markup=False,         # on ne met PAS de markup dans les messages
            log_time_format="[%H:%M:%S]",
        )
    else:
        handler = logging.StreamHandler(stream=stream)
        handler.setLevel(level)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(formatter)

    # Installer sur "trader" (couvre trader.tools.*, trader.daemon, etc.)
    # ET sur "casys-trader" (logger principal du daemon).
    # propagate=False sur les deux pour éviter les doublons vers le root logger.
    for name in ("trader", "casys-trader"):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers.clear()
        lg.addHandler(handler)
        lg.propagate = False

    # Bruit lib tierce : IB Gateway down en paper → ib_async crache des ERROR
    # « API connection failed » / « Make sure API port » à chaque probe. Notre
    # WARNING ib_attach résume déjà l'indisponibilité → on coupe le brut.
    logging.getLogger("ib_async").setLevel(logging.CRITICAL)
