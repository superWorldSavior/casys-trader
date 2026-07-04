"""Agent prompt memory backed by mandate Markdown files.

`mandate/mandate.md` is the human-owned objective and guardrail document.
`mandate/memory.md` is the long-lived strategy memory the agent may append to.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Memory"]


class Memory:
    def __init__(self, mandate_path: str | Path, memory_path: str | Path):
        self.mandate_path = Path(mandate_path)
        self.memory_path = Path(memory_path)

    def read_mandate(self) -> str:
        return self.mandate_path.read_text() if self.mandate_path.exists() else ""

    def read_memory(self) -> str:
        return self.memory_path.read_text() if self.memory_path.exists() else ""

    def append_learning(self, note: str, now: datetime | None = None) -> None:
        """Ajoute une entree horodatee a la memoire agent."""
        now = now or datetime.now(timezone.utc)
        entry = f"\n## {now.isoformat()}\n\n{note.strip()}\n"
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        with self.memory_path.open("a") as f:
            f.write(entry)
