"""tui — TUI live pour casys-trader.

Lit `state/last_report.json` toutes les 2 secondes et affiche en temps réel
l'état du portefeuille, les positions et les dernières décisions.

Usage CLI :
    uv run python -m trader.tui
    # ou directement
    python trader/tui.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from rich.columns import Columns
from rich.console import Console, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Racine du repo (deux niveaux au-dessus de ce fichier)
_ROOT = Path(__file__).resolve().parent.parent
_STATE_FILE = _ROOT / "state" / "last_report.json"
_KILL_FILE = _ROOT / "KILL"


# ---------------------------------------------------------------------------
# Lecture d'état
# ---------------------------------------------------------------------------


def load_state(path: str | Path) -> dict | None:
    """Lit le fichier JSON et retourne le dict, ou None si absent/illisible.

    Ne lève jamais d'exception — conçu pour une boucle de polling.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Construction de la vue (PURE — ne lit aucun fichier)
# ---------------------------------------------------------------------------


def build_view(state: dict | None) -> RenderableType:
    """Construit l'affichage Rich à partir d'un dict d'état.

    Paramètres
    ----------
    state:
        Dict issu de `last_report.json`. Accepte None ou un dict partiel
        sans lever d'exception. Le champ optionnel ``kill_switch`` (bool)
        doit être injecté par l'appelant (non lu depuis le disque ici).
    """
    # Normalisation défensive
    if state is None:
        state = {}

    portfolio: dict = state.get("portfolio") or {}
    holdings: list[dict] = portfolio.get("holdings", [])
    decisions: list[dict] = state.get("decisions", [])
    dry_run: bool = state.get("dry_run", True)
    ts: str = state.get("ts", "—")
    kill_active: bool = state.get("kill_switch", False)
    halted: str | None = state.get("halted")

    # ------------------------------------------------------------------
    # Panel header — équité, cash, rendement, mode
    # ------------------------------------------------------------------
    cash: float = portfolio.get("cash", 0.0)
    equity: float = portfolio.get("equity", 0.0)
    ret_pct: float = portfolio.get("total_return_pct", 0.0)

    mode_label = Text("LIVE", style="bold red") if not dry_run else Text("DRY-RUN", style="bold yellow")

    kill_label: Text
    if kill_active:
        kill_label = Text("KILL ACTIF", style="bold red on white")
    else:
        kill_label = Text("nominal", style="green")

    if halted:
        halted_label = Text(f"  !! HALTED: {halted} !!", style="bold red")
    else:
        halted_label = Text("")

    ret_style = "green" if ret_pct >= 0 else "red"
    header_lines = Text.assemble(
        ("Equity : ", "bold"), (f"${equity:,.2f}   ", "cyan"),
        ("Cash : ", "bold"), (f"${cash:,.2f}   ", "cyan"),
        ("Rendement : ", "bold"), (f"{ret_pct:+.2f}%   ", ret_style),
        ("Mode : ", "bold"), mode_label,
        ("   Kill-switch : ", "bold"), kill_label,
        ("   Dernier cycle : ", "bold"), (ts, "dim"),
        halted_label,
    )

    header_panel = Panel(header_lines, title="[bold]casys-trader — état live[/bold]", expand=True)

    # ------------------------------------------------------------------
    # Table des positions
    # ------------------------------------------------------------------
    pos_table = Table(title="Positions", show_lines=False, expand=True)
    pos_table.add_column("Symbole", style="bold")
    pos_table.add_column("Qté", justify="right")
    pos_table.add_column("Prix moy.", justify="right")
    pos_table.add_column("Dernier prix", justify="right")
    pos_table.add_column("PnL latent", justify="right")

    for h in holdings:
        symbol: str = str(h.get("symbol", "?"))
        qty: float = float(h.get("quantity", 0))
        avg: float = float(h.get("avg_price", 0))
        last: float = float(h.get("last_price", 0))
        pnl: float = float(h.get("unrealized_pnl", 0))
        pnl_style = "green" if pnl >= 0 else "red"
        pos_table.add_row(
            symbol,
            f"{qty:,.4f}",
            f"${avg:,.4f}",
            f"${last:,.4f}",
            Text(f"{pnl:+,.2f}", style=pnl_style),
        )

    if not holdings:
        pos_table.add_row("—", "—", "—", "—", "—")

    # ------------------------------------------------------------------
    # Table des décisions
    # ------------------------------------------------------------------
    dec_table = Table(title="Dernières décisions", show_lines=False, expand=True)
    dec_table.add_column("Symbole", style="bold")
    dec_table.add_column("Action")
    dec_table.add_column("Qté", justify="right")
    dec_table.add_column("Raison")
    dec_table.add_column("Confiance", justify="right")

    for d in decisions:
        action: str = str(d.get("action", "HOLD"))
        action_style = "green" if action == "BUY" else ("red" if action == "SELL" else "dim")
        qty_d: float = float(d.get("qty", 0))
        rationale: str = str(d.get("rationale", ""))
        # Tronquer les raisons longues pour ne pas polluer l'affichage
        if len(rationale) > 60:
            rationale = rationale[:57] + "..."
        confidence: float = float(d.get("confidence", 0))
        dec_table.add_row(
            str(d.get("symbol", "?")),
            Text(action, style=action_style),
            f"{qty_d:,.4f}",
            rationale,
            f"{confidence:.2f}",
        )

    if not decisions:
        dec_table.add_row("—", "—", "—", "—", "—")

    # ------------------------------------------------------------------
    # Assemblage en un seul renderable via Group
    # ------------------------------------------------------------------
    from rich.console import Group  # import local pour éviter la confusion avec Columns

    return Group(header_panel, pos_table, dec_table)


# ---------------------------------------------------------------------------
# Boucle live
# ---------------------------------------------------------------------------


def main() -> None:
    """Lance le TUI en mode live. Ctrl+C pour quitter proprement."""
    console = Console()

    try:
        with Live(console=console, refresh_per_second=1, screen=False) as live:
            while True:
                raw = load_state(_STATE_FILE)
                # Injection de l'état du kill-switch (lecture fichier dans main, pas dans build_view)
                kill_active = _KILL_FILE.exists()
                if isinstance(raw, dict):
                    raw = {**raw, "kill_switch": kill_active}
                else:
                    raw = {"kill_switch": kill_active}
                live.update(build_view(raw))
                time.sleep(2.0)
    except KeyboardInterrupt:
        console.print("[yellow]TUI arrêté.[/yellow]")


if __name__ == "__main__":
    main()
