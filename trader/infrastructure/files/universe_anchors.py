"""Read current universe symbols as logical market anchors."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from trader.market.rotation.wiring import venue_of


class YamlUniverseAnchorSource:
    def __init__(
        self,
        path: str | Path,
        *,
        venue_of_fn: Callable[[str], str] = venue_of,
    ) -> None:
        resolved = Path(path)
        self._path = resolved / "universe.yaml" if resolved.is_dir() else resolved
        self._venue_of = venue_of_fn

    def current_anchors(self) -> tuple[tuple[str, str], ...]:
        try:
            payload = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except OSError:
            return ()
        if not isinstance(payload, dict):
            return ()
        anchors: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in payload.get("symbols") or ():
            symbol = str(raw or "").strip()
            if not symbol:
                continue
            venue = str(self._venue_of(symbol) or "").strip() or "US"
            key = (venue, symbol)
            if key in seen:
                continue
            seen.add(key)
            anchors.append(key)
        return tuple(anchors)


__all__ = ["YamlUniverseAnchorSource"]
