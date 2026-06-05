"""scheduler — l'agent pilote son propre prochain réveil.

L'infra ne décide PAS de la cadence. L'agent appelle `set_next_wake(...)` à la fin
de chaque cycle ; le daemon lit `next_wake()` pour savoir quand se relancer. État
persisté pour survivre aux redémarrages.

Le temps est injecté (`now`) pour rester déterministe et testable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Scheduler:
    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)

    def set_next_wake(self, when_iso: str) -> None:
        """Fixe le prochain réveil (timestamp ISO 8601, UTC de préférence)."""
        datetime.fromisoformat(when_iso)  # fail fast si format invalide
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"next_wake": when_iso}))

    def set_next_wake_in(self, *, minutes: float, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        when = (now + timedelta(minutes=minutes)).isoformat()
        self.set_next_wake(when)
        return when

    def next_wake(self) -> datetime | None:
        if not self.state_path.exists():
            return None
        raw = json.loads(self.state_path.read_text())
        return datetime.fromisoformat(raw["next_wake"])

    def seconds_until_wake(self, now: datetime | None = None) -> float:
        """Secondes avant le prochain réveil. 0 si dû/non planifié (réveil immédiat)."""
        nxt = self.next_wake()
        if nxt is None:
            return 0.0
        now = now or datetime.now(timezone.utc)
        return max(0.0, (nxt - now).total_seconds())
