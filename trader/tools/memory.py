"""memory — stratégie et learnings persistants de l'agent.

Fichiers markdown du repo : `mandate/mandate.md` (objectif + marchés, édité en
boucle 1) et `mandate/memory.md` (l'agent y écrit sa stratégie évolutive). Le
daemon lit les deux au réveil ; l'agent append ses learnings.

Markdown volontaire : éditable par un humain ET par l'agent, versionnable en git.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class LearningsStore:
    """Mémoire de travail machine-owned : learnings runtime de l'agent (JSONL borné).

    Séparée de `memory.md` (édité par la boucle 1, humaine). Le daemon append ici
    les notes que l'agent émet à chaque réveil ; les N dernières sont réinjectées
    dans le contexte du réveil suivant. Bornée par `max_entries` pour que le
    fichier — et le contexte — ne gonflent jamais. La boucle 1 distille
    périodiquement ce flux vers `memory.md`.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = 200,
        archive_path: str | Path | None = None,
    ):
        self.path = Path(path)
        self.max_entries = max_entries
        # Archive append-only des évincés : le rolling buffer ne JETTE plus
        # (chantier learnings 2026-07-02 — 89 % des notes partaient à la poubelle).
        self.archive_path = (
            Path(archive_path)
            if archive_path is not None
            else self.path.parent / "archive" / f"{self.path.stem}-evicted.jsonl"
        )

    def _read_rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def append(self, *, symbol: str, note: str, now: datetime | None = None, **extra: object) -> bool:
        """Append une note. Retourne True si écrite, False si vide/non bornée.

        Tronque le fichier à `max_entries` (write atomique : temp + os.replace,
        pour qu'un crash en cours d'écriture ne corrompe pas le store)."""
        note = note.strip()
        if not note or self.max_entries <= 0:
            return False
        now = now or datetime.now(timezone.utc)
        row = {"ts": now.isoformat(), "symbol": symbol, "note": note, **extra}
        rows = self._read_rows()
        rows.append(row)
        evicted = rows[: -self.max_entries] if len(rows) > self.max_entries else []
        rows = rows[-self.max_entries :]
        if evicted:
            self._archive_evicted(evicted, now=now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for item in rows:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)
        return True

    def _archive_evicted(self, evicted: list[dict], *, now: datetime) -> None:
        """Append-only, jamais relu par le runtime — matière première du futur
        recall/RAG. `ts` d'origine conservé, `evicted_at` ajouté pour l'audit."""
        try:
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            with self.archive_path.open("a", encoding="utf-8") as f:
                for item in evicted:
                    f.write(json.dumps({**item, "evicted_at": now.isoformat()}, ensure_ascii=False) + "\n")
        except OSError as exc:
            # L'archive est best-effort : son échec ne doit jamais bloquer
            # l'écriture du buffer vif (le daemon live en dépend).
            log.warning("archive évincés non écrite %s (%s)", self.archive_path, exc)

    def recent(self, limit: int = 10) -> list[dict]:
        """Les `limit` entrées les plus récentes, dans l'ordre chronologique."""
        if limit <= 0:
            return []
        return self._read_rows()[-limit:]

    def all(self) -> list[dict]:
        """Toutes les entrées du buffer brut, dans l'ordre chronologique."""
        return self._read_rows()


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
