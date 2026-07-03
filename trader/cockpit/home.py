"""Home « mode Gonzo » du cockpit : builders purs + widgets Textual.

Spec : docs/superpowers/specs/2026-07-03-cockpit-home-gonzo-design.md.
Les builders sont purs (state dict → renderable) ; seuls les widgets
touchent Textual. Aucune I/O ici.
"""
from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text

from trader.cockpit.aggregates import venue_clock
from trader.read_models.runtime_state import _safe_float
from trader.ui.palette import Palette
from trader.ui.rich_panels import sparkline

UTC = timezone.utc

# Ordre de drop quand la largeur manque — les 4 segments critiques
# (vital, équité, mode, kill) ne figurent volontairement pas ici.
_STATUS_DROP_ORDER = ("spark", "venues", "pnl_usd", "cycle", "utc", "llm", "pnl_pct")


def _vital_text(vital, palette: Palette) -> Text:
    if vital.status == "alive":
        if vital.battement_old:
            mins = int(vital.since_seconds or 0) // 60
            secs = int(vital.since_seconds or 0) % 60
            return Text.assemble(("● VIVANT", "bold yellow"),
                                 (f" {mins:02d}:{secs:02d}", palette["dim"]))
        return Text("● VIVANT", style="bold green")
    if vital.status == "stopped":
        return Text("● ARRÊTÉ", style="bold red")
    return Text("● jamais démarré", style=palette["dim"])


def build_status_line(
    state: dict,
    *,
    kill_active: bool,
    palette: Palette,
    width: int,
    now: datetime,
    vital,
) -> Text:
    """Barre de statut distillée. Pur : vital et now sont injectés."""
    state = state if isinstance(state, dict) else {}
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    daemon_status = (
        state.get("daemon_status") if isinstance(state.get("daemon_status"), dict) else {}
    )

    equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
    cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
    starting = _safe_float(state.get("starting_cash"), default=cash) or cash
    pnl = equity - starting
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        ret_pct = (_safe_float(kpis.get("total_return"), default=0.0) or 0.0) * 100.0
    pnl_style = palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]

    curve = [
        v for v in (_safe_float(x, default=None) for x in (state.get("equity_curve") or []))
        if v is not None
    ]
    spark = sparkline(curve[-24:]) if len(curve) >= 2 else ""

    used = daemon_status.get("model_calls_used")
    max_calls = daemon_status.get("max_model_calls_per_cycle")
    llm = f"LLM {used}/{max_calls}" if used is not None and max_calls is not None else "LLM —"

    done = daemon_status.get("decisions_done")
    total = daemon_status.get("symbols_total")
    cycle_running = (
        isinstance(total, int) and total > 0 and isinstance(done, int) and done < total
    )

    clock = venue_clock(state.get("sessions") or {}, now)
    venue_bits: list[tuple[str, str]] = []
    for venue in clock.open_now:
        venue_bits.append((f"{venue}●", palette["status_nominal"]))
        venue_bits.append((" ", ""))
    if clock.next_at is not None and clock.next_venue not in clock.open_now:
        venue_bits.append(
            (f"{clock.next_venue} {clock.next_at.strftime('%H:%M')}", palette["dim"])
        )
    venues = Text.assemble(*venue_bits) if venue_bits else None

    mode = Text("LIVE", style="bold red") if not state.get("dry_run", True) else Text(
        "DRY", style="bold yellow"
    )
    kill = (
        Text(" !! KILL !! ", style="bold white on red")
        if kill_active
        else Text.assemble(("kill:", palette["dim"]), ("nominal", palette["status_nominal"]))
    )

    segments: list[tuple[str, Text | None]] = [
        ("vital", _vital_text(vital, palette)),
        ("equity", Text(f"{equity:,.0f}$", style=f"bold {palette['status_equity']}")),
        ("spark", Text(spark, style=palette["equity_line"]) if spark else None),
        ("pnl_pct", Text(f"{ret_pct:+.2f}%", style=pnl_style)),
        ("pnl_usd", Text(f"({pnl:+,.0f})", style=pnl_style)),
        ("mode", mode),
        ("kill", kill),
        ("llm", Text(llm, style=palette["status_accent"])),
        ("cycle", Text(f"cycle {done}/{total}", style=palette["status_accent"])
         if cycle_running else None),
        ("venues", venues),
        ("utc", Text(now.strftime("%H:%M:%SZ"), style=palette["dim"])),
    ]
    kept = [(name, text) for name, text in segments if text is not None]

    def _assemble(parts: list[tuple[str, Text]]) -> Text:
        out = Text("  ")
        for index, (_, piece) in enumerate(parts):
            if index:
                out.append(" · ", style=palette["dim"])
            out.append_text(piece)
        return out

    line = _assemble(kept)
    for drop in _STATUS_DROP_ORDER:
        if line.cell_len <= width:
            break
        kept = [(name, text) for name, text in kept if name != drop]
        line = _assemble(kept)
    return line
