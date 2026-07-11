"""Indicator watch Rich builders."""

from __future__ import annotations

from datetime import datetime

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from trader.domain.planning.watches import is_armed_plan as _is_armed_plan
from trader.interfaces.ui.palette import PALETTE_DARK, Palette
from trader.interfaces.ui.panels.common import _expire_relative
from trader.reporting.read_models.runtime_state import (
    UTC,
    _safe_list_of_dicts,
)


def _build_watches_panel(
    watches: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    """Veilles actives (colonne droite, sous logs).

    Source : _load_indicator_watches_safe(state/scheduler.json).
    Les plans armés (EXECUTE_ORDER + order) ont leur propre panneau : exclus ici.
    """
    watches = [w for w in watches if not _is_armed_plan(w)]
    if not watches:
        return Panel(
            Text("aucune veille active", style=palette["dim"]),
            title="[bold]Veilles[/bold]",
            border_style=palette["border_watches"],
            expand=True,
        )

    now_utc = datetime.now(UTC)
    watch_lines: list[Text] = []
    for watch in watches:
        symbol = str(watch.get("symbol", "?"))
        expires_raw = str(watch.get("expires_at") or "")
        logic = str(watch.get("logic", "any"))
        conditions = _safe_list_of_dicts(watch.get("conditions") or [])

        expire_str = _expire_relative(expires_raw, now=now_utc)

        # Conditions compactes
        cond_parts: list[str] = []
        for cond in conditions[:3]:  # max 3 conditions affichées
            ind = str(cond.get("indicator") or "?")
            op = str(cond.get("op") or "?")
            val = cond.get("value")
            tf = str(cond.get("timeframe") or cond.get("interval") or "?")
            val_str = f"{val}" if val is not None else "?"
            cond_parts.append(f"{ind}{op}{val_str}@{tf}")
        cond_str = f" [{logic}] ".join(cond_parts) if cond_parts else "?"

        line = Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (cond_str, palette["dim"]),
            ("  ", ""),
            (expire_str, palette["kpi_vol_warn"]),
        )
        watch_lines.append(line)

    return Panel(
        Group(*watch_lines),
        title="[bold]Veilles[/bold]",
        border_style=palette["border_watches"],
        expand=True,
    )


__all__ = ["_build_watches_panel"]
