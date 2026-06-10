"""tui — TUI live pour casys-trader.

Lit `state/current_report.json` puis `state/last_report.json` toutes les 2
secondes et affiche l'état du portefeuille, les positions et le daemon.

Usage CLI :
    uv run python -m trader.tui
    # ou directement
    python trader/tui.py
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.columns import Columns
from rich.console import Console, RenderableType
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trader.palette import PALETTE_DARK, Palette

# Racine du repo (deux niveaux au-dessus de ce fichier)
_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _ROOT / "state"
_CURRENT_REPORT_FILE = _STATE_DIR / "current_report.json"
_LAST_REPORT_FILE = _STATE_DIR / "last_report.json"
_STATUS_FILE = _STATE_DIR / "daemon_status.json"
_KILL_FILE = _ROOT / "KILL"

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


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


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    """Convertit en float fini, sinon retourne ``default``."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _safe_list_of_dicts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _format_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _load_equity_curve(history_path: Path) -> list[float]:
    """Lit les points d'équité non nuls depuis history.jsonl, sans lever."""
    if not history_path.exists():
        return []
    values: list[float] = []
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict) or row.get("equity") is None:
            continue
        equity = _safe_float(row.get("equity"), default=None)
        if equity is not None:
            values.append(equity)
    return values


def _compute_live_kpis_safe(state_dir: Path) -> dict:
    try:
        from trader.stats import compute_live_kpis

        result = compute_live_kpis(state_dir)
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def _compute_attribution_safe(state_dir: Path) -> dict:
    try:
        from trader.attribution import compute_attribution

        result = compute_attribution(state_dir)
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def _load_learnings_safe(state_dir: Path, *, limit: int = 5) -> list[dict]:
    try:
        from trader.tools.memory import LearningsStore

        return _safe_list_of_dicts(
            LearningsStore(state_dir / "learnings.jsonl").recent(limit=limit)
        )
    except Exception:
        return []


def load_runtime_state(
    *,
    state_dir: str | Path = _STATE_DIR,
    current_report_path: str | Path | None = None,
    last_report_path: str | Path | None = None,
    status_path: str | Path | None = None,
) -> dict:
    """Charge le meilleur état affichable sans lever d'exception."""
    state_dir_path = Path(state_dir)
    current_report_path = (
        Path(current_report_path)
        if current_report_path is not None
        else state_dir_path / "current_report.json"
    )
    last_report_path = (
        Path(last_report_path)
        if last_report_path is not None
        else state_dir_path / "last_report.json"
    )
    status_path = (
        Path(status_path)
        if status_path is not None
        else state_dir_path / "daemon_status.json"
    )

    source = "none"
    raw = load_state(current_report_path)
    if isinstance(raw, dict):
        source = "current_report"
    else:
        raw = load_state(last_report_path)
        if isinstance(raw, dict):
            source = "last_report"
        else:
            raw = {}

    status = load_state(status_path)
    kpis = _compute_live_kpis_safe(state_dir_path)
    attribution = _compute_attribution_safe(state_dir_path)
    equity_curve = _load_equity_curve(state_dir_path / "history.jsonl")
    learnings = _load_learnings_safe(state_dir_path)
    return {
        **raw,
        "source": source,
        "daemon_status": status if isinstance(status, dict) else {},
        "kpis": kpis
        if kpis
        else (raw.get("kpis") if isinstance(raw.get("kpis"), dict) else {}),
        "attribution": attribution
        if attribution
        else (
            raw.get("attribution") if isinstance(raw.get("attribution"), dict) else {}
        ),
        "equity_curve": equity_curve,
        "learnings": learnings,
    }


# ---------------------------------------------------------------------------
# Construction de la vue (PURE — ne lit aucun fichier)
# ---------------------------------------------------------------------------


def sparkline(values: list[float]) -> str:
    """Mini-courbe unicode à 8 niveaux. Retourne "" si aucune valeur valide."""
    clean = [_safe_float(value, default=None) for value in values]
    clean_values = [value for value in clean if value is not None]
    if not clean_values:
        return ""

    low = min(clean_values)
    high = max(clean_values)
    if high == low:
        return "▄" * len(clean_values)

    span = high - low
    last_index = len(_SPARK_BLOCKS) - 1
    blocks: list[str] = []
    for value in clean_values:
        index = int(round((value - low) / span * last_index))
        index = max(0, min(last_index, index))
        blocks.append(_SPARK_BLOCKS[index])
    return "".join(blocks)


def _fmt_money(value: Any, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"${number:,.2f}"


def _fmt_signed_money(value: Any, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number:+,.2f}"


def _fmt_number(value: Any, decimals: int = 2, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number:.{decimals}f}"


def _fmt_percent(value: Any, decimals: int = 1, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number * 100.0:.{decimals}f}%"


def _fmt_int(value: Any, *, default: str = "0") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{int(number)}"


def _style_for_pnl(value: Any) -> str:
    number = _safe_float(value, default=0.0) or 0.0
    return "green" if number >= 0 else "red"


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _build_kpi_band(kpis: dict, *, palette: Palette = PALETTE_DARK) -> RenderableType:
    sharpe = _safe_float(kpis.get("sharpe"), default=None)
    max_dd = _safe_float(kpis.get("max_drawdown"), default=None)
    win_rate = _safe_float(kpis.get("period_win_rate"), default=None)
    volatility = _safe_float(kpis.get("volatility"), default=None)
    trades = kpis.get("num_trades")

    def card(title: str, value: str, border: str, subtitle: str = "") -> Panel:
        body = Text.assemble((value, f"bold {border}"))
        if subtitle:
            body.append("\n")
            body.append(subtitle, style=palette["dim"])
        return Panel(
            body, title=f"[bold]{title}[/bold]", border_style=border, expand=True
        )

    sharpe_style = (
        palette["kpi_sharpe_ok"]
        if (sharpe is not None and sharpe >= 1.0)
        else (
            palette["kpi_sharpe_bad"] if (sharpe or 0.0) < 0 else palette["kpi_default"]
        )
    )
    win_style = (
        palette["kpi_sharpe_ok"]
        if (win_rate is not None and win_rate >= 0.5)
        else (
            palette["kpi_sharpe_bad"]
            if win_rate is not None
            else palette["kpi_default"]
        )
    )
    vol_style = (
        palette["kpi_default"]
        if volatility is None or volatility <= 0.25
        else palette["kpi_vol_warn"]
    )

    return Columns(
        [
            card("Sharpe", _fmt_number(sharpe), sharpe_style, "qualité risque"),
            card("Max DD", _fmt_percent(max_dd), palette["kpi_sharpe_bad"], "drawdown"),
            card("Win rate", _fmt_percent(win_rate), win_style, "période"),
            card("Volatilité", _fmt_percent(volatility), vol_style, "annualisée"),
            card(
                "Trades", _fmt_int(trades), palette["kpi_default"], "clôturés/exécutés"
            ),
        ],
        equal=True,
        expand=True,
    )


def _build_equity_panel(
    values: list[float], *, palette: Palette = PALETTE_DARK
) -> Panel:
    if len(values) < 2:
        return Panel(
            Text(
                "Courbe indisponible : moins de 2 points d'équité.",
                style=palette["equity_dim"],
            ),
            title="[bold]Équité[/bold]",
            border_style=palette["border_default"],
            expand=True,
        )

    try:
        import plotext as plt

        plt.clear_figure()
        plt.theme("pro")
        plt.plotsize(70, 12)
        plt.plot(list(range(len(values))), values, marker="braille", color="cyan")
        plt.title("Courbe d'équité")
        plt.xlabel("cycle")
        plt.ylabel("équité")
        chart: RenderableType = Text.from_ansi(plt.build())
    except Exception:
        chart = Text(sparkline(values[-70:]), style=f"bold {palette['equity_line']}")

    return Panel(
        chart,
        title="[bold]Équité[/bold]",
        border_style=palette["border_default"],
        expand=True,
    )


def _build_positions_panel(
    holdings: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    pos_table = Table(show_lines=False, expand=True)
    pos_table.add_column("Symbole", style="bold")
    pos_table.add_column("Qté", justify="right")
    pos_table.add_column("Prix moy.", justify="right")
    pos_table.add_column("Dernier prix", justify="right")
    pos_table.add_column("PnL latent", justify="right")
    pos_table.add_column("PnL %", justify="right")

    for h in holdings:
        symbol = str(h.get("symbol", "?"))
        qty = _safe_float(h.get("quantity"), default=0.0) or 0.0
        avg = _safe_float(h.get("avg_price"), default=0.0) or 0.0
        last = _safe_float(h.get("last_price"), default=0.0) or 0.0
        pnl = _safe_float(h.get("unrealized_pnl"), default=0.0) or 0.0
        notional = abs(avg * qty)
        pnl_pct = (pnl / notional * 100.0) if notional else 0.0
        pnl_style = palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]
        pos_table.add_row(
            symbol,
            f"{qty:,.4f}",
            f"${avg:,.4f}",
            f"${last:,.4f}",
            Text(f"{pnl:+,.2f}", style=pnl_style),
            Text(f"{pnl_pct:+.2f}%", style=pnl_style),
        )

    if not holdings:
        pos_table.add_row("—", "—", "—", "—", "—", "—")

    return Panel(
        pos_table,
        title="[bold]Positions[/bold]",
        border_style=palette["border_default"],
        expand=True,
    )


def _confidence_bar(
    win_rate: Any, pnl: Any, *, width: int = 12, palette: Palette = PALETTE_DARK
) -> Text:
    win = _safe_float(win_rate, default=0.0) or 0.0
    win = max(0.0, min(1.0, win))
    filled = int(round(win * width))
    bar = "█" * filled + "░" * (width - filled)
    pnl_val = _safe_float(pnl, default=0.0) or 0.0
    return Text(
        bar, style=palette["pnl_positive"] if pnl_val >= 0 else palette["pnl_negative"]
    )


def _build_attribution_panel(
    attribution: dict, *, palette: Palette = PALETTE_DARK
) -> Panel:
    realized_pnl = _safe_float(attribution.get("realized_pnl"), default=0.0) or 0.0
    pnl_style = (
        palette["pnl_positive"] if realized_pnl >= 0 else palette["pnl_negative"]
    )
    summary = Text.assemble(
        ("Trades clôturés : ", "bold"),
        (_fmt_int(attribution.get("n_closed_trades")), palette["kpi_default"]),
        ("   P&L réalisé : ", "bold"),
        (_fmt_signed_money(realized_pnl), pnl_style),
        ("   Win rate : ", "bold"),
        (_fmt_percent(attribution.get("win_rate")), palette["kpi_default"]),
        ("   Détention moy. : ", "bold"),
        (
            _fmt_number(attribution.get("avg_holding_minutes"), 1),
            palette["kpi_default"],
        ),
        (" min", palette["dim"]),
    )

    confidence_rows = _safe_list_of_dicts(attribution.get("by_confidence"))
    confidence_table = Table.grid(expand=True)
    confidence_table.add_column(ratio=2)
    confidence_table.add_column(ratio=2)
    confidence_table.add_column(ratio=3, justify="right")
    if confidence_rows:
        for row in confidence_rows:
            pnl = _safe_float(row.get("total_pnl"), default=0.0) or 0.0
            row_pnl_style = (
                palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]
            )
            confidence_table.add_row(
                Text(str(row.get("bucket", "—")), style="bold"),
                _confidence_bar(row.get("win_rate"), pnl, palette=palette),
                Text(
                    f"n={_fmt_int(row.get('n'))}  win={_fmt_percent(row.get('win_rate'))}  P&L {_fmt_signed_money(pnl)}",
                    style=row_pnl_style,
                ),
            )
    else:
        confidence_table.add_row(Text("—", style=palette["dim"]), Text(""), Text(""))

    exit_rows = _safe_list_of_dicts(attribution.get("by_exit_reason"))
    exit_table = Table(
        show_header=True,
        header_style=f"bold {palette['dim']}",
        box=None,
        expand=True,
        pad_edge=False,
    )
    exit_table.add_column("Raison")
    exit_table.add_column("n", justify="right")
    exit_table.add_column("Win", justify="right")
    exit_table.add_column("P&L", justify="right")
    if exit_rows:
        for row in exit_rows:
            pnl = _safe_float(row.get("total_pnl"), default=0.0) or 0.0
            exit_table.add_row(
                str(row.get("reason", "—")),
                _fmt_int(row.get("n")),
                _fmt_percent(row.get("win_rate")),
                Text(
                    _fmt_signed_money(pnl),
                    style=palette["pnl_positive"]
                    if pnl >= 0
                    else palette["pnl_negative"],
                ),
            )
    else:
        exit_table.add_row("—", "—", "—", "—")

    return Panel(
        Group(
            summary,
            Text("Calibration confiance", style="bold"),
            confidence_table,
            Text("Raisons de sortie", style="bold"),
            exit_table,
        ),
        title="[bold]Attribution[/bold]",
        border_style=palette["border_attribution"],
        expand=True,
    )


def _build_decisions_table(
    decisions: list[dict], *, palette: Palette = PALETTE_DARK
) -> Table:
    dec_table = Table(title="Dernières décisions", show_lines=False, expand=True)
    dec_table.add_column("Symbole", style="bold")
    dec_table.add_column("Action")
    dec_table.add_column("Qté", justify="right")
    dec_table.add_column("Raison", overflow="fold", ratio=4)
    dec_table.add_column("Confiance", justify="right")

    for d in decisions:
        action = str(d.get("action", "HOLD"))
        action_style = (
            palette["action_buy"]
            if action == "BUY"
            else (
                palette["action_sell"] if action == "SELL" else palette["action_hold"]
            )
        )
        qty_d = _safe_float(d.get("qty"), default=0.0) or 0.0
        rationale = str(d.get("rationale") or "")
        confidence = _safe_float(d.get("confidence"), default=0.0) or 0.0
        dec_table.add_row(
            str(d.get("symbol", "?")),
            Text(action, style=action_style),
            f"{qty_d:,.4f}",
            rationale,
            f"{confidence:.2f}",
        )

    if not decisions:
        dec_table.add_row("—", "—", "—", "—", "—")

    return dec_table


def _build_learnings_panel(
    learnings: list[dict], *, palette: Palette = PALETTE_DARK
) -> RenderableType | None:
    if not learnings:
        return None
    lines: list[Text] = []
    for index, item in enumerate(learnings[-5:]):
        if index:
            lines.append(Text(""))
        note = str(item.get("note") or "")
        symbol = str(item.get("symbol") or "—")
        ts = _format_datetime(item.get("ts"))
        lines.append(
            Text.assemble(
                ("▸ ", palette["dim"]),
                (symbol, palette["learning_symbol"]),
                ("  ·  ", palette["dim"]),
                (ts, palette["dim"]),
            )
        )
        lines.append(Text.assemble(("  ", palette["dim"]), note))
    return Panel(
        Group(*lines),
        title="[bold]Derniers apprentissages[/bold]",
        border_style=palette["border_learnings"],
        expand=True,
    )


def build_view(
    state: dict | None, *, palette: Palette = PALETTE_DARK
) -> RenderableType:
    """Construit l'affichage Rich à partir d'un dict d'état.

    Paramètres
    ----------
    state:
        Dict issu de `load_runtime_state`. Accepte None ou un dict partiel sans
        lever d'exception. Le champ optionnel ``kill_switch`` (bool) doit être
        injecté par l'appelant (non lu depuis le disque ici).
    palette:
        Design tokens Rich. Défaut PALETTE_DARK → comportement identique à l'existant.
    """
    # Normalisation défensive
    if not isinstance(state, dict):
        state = {}

    portfolio = (
        state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    )
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    attribution = (
        state.get("attribution") if isinstance(state.get("attribution"), dict) else {}
    )
    holdings = _safe_list_of_dicts(portfolio.get("holdings"))
    decisions = _safe_list_of_dicts(state.get("decisions"))
    daemon_status = (
        state.get("daemon_status")
        if isinstance(state.get("daemon_status"), dict)
        else {}
    )
    equity_curve = [
        _safe_float(value, default=None) for value in (state.get("equity_curve") or [])
    ]
    equity_curve = [value for value in equity_curve if value is not None]
    learnings = _safe_list_of_dicts(state.get("learnings"))
    dry_run: bool = state.get("dry_run", True)
    ts = _format_datetime(state.get("ts"))
    source: str = str(state.get("source", "—"))
    kill_active: bool = state.get("kill_switch", False)
    halted: str | None = state.get("halted")

    # ------------------------------------------------------------------
    # Panel header — équité, cash, rendement, mode
    # ------------------------------------------------------------------
    cash = _safe_float(portfolio.get("cash"), default=None)
    if cash is None:
        cash = _safe_float(kpis.get("cash"), default=0.0) or 0.0
    equity = _safe_float(portfolio.get("equity"), default=None)
    if equity is None:
        equity = _safe_float(kpis.get("equity"), default=0.0) or 0.0
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        total_return = _safe_float(kpis.get("total_return"), default=0.0) or 0.0
        ret_pct = total_return * 100.0
    unrealized_total = sum(
        _safe_float(h.get("unrealized_pnl"), default=0.0) or 0.0 for h in holdings
    )
    phase = str(daemon_status.get("phase", "—"))
    current_symbol = str(daemon_status.get("current_symbol") or "—")
    done = daemon_status.get("decisions_done")
    total = daemon_status.get("symbols_total")
    progress = f"{done}/{total}" if done is not None and total is not None else "—"
    used = daemon_status.get("model_calls_used")
    limit = daemon_status.get("max_model_calls_per_cycle")
    calls = (
        f"{used}/{limit}"
        if used is not None and limit is not None
        else (str(used) if used is not None else "—")
    )

    mode_label = (
        Text("LIVE", style="bold red")
        if not dry_run
        else Text("DRY-RUN", style="bold yellow")
    )

    kill_label: Text
    if kill_active:
        kill_label = Text("KILL ACTIF", style="bold red on white")
    else:
        kill_label = Text("nominal", style=palette["pnl_positive"])

    if halted:
        halted_label = Text(f"  !! HALTED: {halted} !!", style=palette["pnl_negative"])
    else:
        halted_label = Text("")

    ret_style = palette["pnl_positive"] if ret_pct >= 0 else palette["pnl_negative"]
    unrealized_style = (
        palette["pnl_positive"] if unrealized_total >= 0 else palette["pnl_negative"]
    )
    inline_curve = sparkline(equity_curve[-32:]) if equity_curve else ""
    header_lines = Text.assemble(
        ("Équité : ", "bold"),
        (f"${equity:,.2f}", f"bold {palette['kpi_default']}"),
        (f"  {inline_curve}   " if inline_curve else "   ", palette["kpi_default"]),
        ("Cash : ", "bold"),
        (f"${cash:,.2f}   ", palette["kpi_default"]),
        ("Rendement : ", "bold"),
        (f"{ret_pct:+.2f}%   ", ret_style),
        ("PnL latent total : ", "bold"),
        (f"{unrealized_total:+,.2f}   ", unrealized_style),
        ("Mode : ", "bold"),
        mode_label,
        ("   Kill-switch : ", "bold"),
        kill_label,
        ("   Dernier cycle : ", "bold"),
        (ts, palette["dim"]),
        halted_label,
        "\n",
        ("Daemon : ", "bold"),
        (phase, palette["status_phase"]),
        ("   Symbole : ", "bold"),
        (current_symbol, palette["kpi_default"]),
        ("   Progrès : ", "bold"),
        (progress, palette["kpi_default"]),
        ("   Appels : ", "bold"),
        (calls, palette["kpi_default"]),
        ("   Source : ", "bold"),
        (source, palette["dim"]),
    )

    header_panel = Panel(
        header_lines, title="[bold]casys-trader — cockpit trading[/bold]", expand=True
    )
    body = Columns(
        [
            _build_positions_panel(holdings, palette=palette),
            _build_attribution_panel(attribution, palette=palette),
        ],
        equal=True,
        expand=True,
    )
    learnings_panel = _build_learnings_panel(learnings, palette=palette)
    footer = (
        Group(_build_decisions_table(decisions, palette=palette), learnings_panel)
        if learnings_panel is not None
        else _build_decisions_table(decisions, palette=palette)
    )

    return Group(
        header_panel,
        _build_kpi_band(kpis, palette=palette),
        _build_equity_panel(equity_curve, palette=palette),
        body,
        footer,
    )


# ---------------------------------------------------------------------------
# Boucle live
# ---------------------------------------------------------------------------


def main() -> None:
    """Lance le TUI en mode live. Ctrl+C pour quitter proprement."""
    console = Console()

    try:
        with Live(console=console, refresh_per_second=1, screen=False) as live:
            while True:
                raw = load_runtime_state()
                # Injection de l'état du kill-switch (lecture fichier dans main, pas dans build_view)
                kill_active = _KILL_FILE.exists()
                raw = {**raw, "kill_switch": kill_active}
                live.update(build_view(raw))
                time.sleep(2.0)
    except KeyboardInterrupt:
        console.print("[yellow]TUI arrêté.[/yellow]")


if __name__ == "__main__":
    main()
