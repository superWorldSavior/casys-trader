"""Journal append-only des décisions de composition d'univers (hot-set).

Distinct du ledger par-trade : ce module trace les *rotations* — quels
symboles entrent/sortent du hot-set à chaque cycle de décision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def log_rotation(
    path: Path | str,
    *,
    as_of: str,
    default_hot: set[str],
    final_hot: set[str],
    sticky: set[str],
    overrides: dict[str, Any],
    rejects: dict[str, Any],
    alerts: list[Any],
) -> None:
    """Append une entrée JSONL de rotation d'univers.

    Crée le dossier parent si nécessaire.
    `as_of` est fourni par l'appelant — pas d'horloge ici.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "source": "rotation",
        "as_of": as_of,
        "default_hot_set": list(default_hot),
        "final_hot_set": list(final_hot),
        "sticky": sorted(sticky),
        "overrides": overrides,
        "rejects": rejects,
        "alerts": list(alerts),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
