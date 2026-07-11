"""Streaming JSONL adapter for model-performance rows."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JsonlModelPerformanceReader:
    path: Path

    def read_rows(self) -> Iterator[Mapping[str, object]]:
        try:
            stream = self.path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return

        with stream:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row


__all__ = ["JsonlModelPerformanceReader"]
