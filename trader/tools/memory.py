"""memory — stratégie et learnings persistants de l'agent.

Fichiers markdown du repo : `mandate/mandate.md` (objectif + marchés, édité en
boucle 1) et `mandate/memory.md` (l'agent y écrit sa stratégie évolutive). Le
daemon lit les deux au réveil ; l'agent append ses learnings.

Markdown volontaire : éditable par un humain ET par l'agent, versionnable en git.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class Memory:
    def __init__(self, mandate_path: str | Path, memory_path: str | Path):
        self.mandate_path = Path(mandate_path)
        self.memory_path = Path(memory_path)

    def read_mandate(self) -> str:
        return self.mandate_path.read_text() if self.mandate_path.exists() else ""

    def read_memory(self) -> str:
        return self.memory_path.read_text() if self.memory_path.exists() else ""

    def append_learning(self, note: str, now: datetime | None = None) -> None:
        """Ajoute une entrée horodatée à la mémoire (l'agent raconte ce qu'il retient)."""
        now = now or datetime.now(timezone.utc)
        entry = f"\n## {now.isoformat()}\n\n{note.strip()}\n"
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        with self.memory_path.open("a") as f:
            f.write(entry)
