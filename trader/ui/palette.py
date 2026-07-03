"""palette — design tokens pour les renderables Rich du cockpit casys-trader.

Deux variantes :
- PALETTE_DARK  : couleurs actuelles de tui.py (fond sombre) — ne jamais modifier.
- PALETTE_LIGHT : déclinaison saumon FT editorial (fond #FFF1E5).
  Accent structurel : bleu pétrole #0D7680 (toutes bordures/titres) —
  distinct du vert #1A6B2F réservé aux gains/BUY.

Les builders de tui.py acceptent ``palette: Palette = PALETTE_DARK`` en argument
optionnel → le comportement par défaut est identique au pixel près.
"""

from __future__ import annotations

from typing import TypedDict


class Palette(TypedDict):
    """Ensemble complet des design tokens Rich pour les builders de tui.py et cockpit.py."""

    # Bordures panneaux
    border_default: str  # positions, equity, kpi cards
    border_attribution: str  # attribution
    border_learnings: str  # learnings

    # Nouveaux panneaux v2
    border_plans: str  # plans de sortie ouverts
    border_watches: str  # veilles actives
    border_llm_activity: str  # activité LLM

    # Graphique équité
    equity_line: str  # couleur sparkline / plotext
    equity_dim: str  # texte dim sur graphique

    # Styles KPI cards
    kpi_sharpe_ok: str
    kpi_sharpe_bad: str
    kpi_default: str  # cyan en DARK
    kpi_vol_warn: str  # yellow en DARK

    # Styles P&L / positions
    pnl_positive: str
    pnl_negative: str
    symbol_bold: str  # bold + couleur pour les symboles

    # Actions décisions
    action_buy: str
    action_sell: str
    action_hold: str

    # Learnings
    learning_symbol: str

    # Utilitaires
    dim: str  # style dim générique

    # CockpitStatus (barre de statut bandeau supérieur)
    status_equity: str  # bold cyan en DARK
    status_accent: str  # cyan pour cash/symbole/appels en DARK
    status_phase: str  # magenta pour phase daemon en DARK
    status_nominal: str  # green pour état nominal kill-switch en DARK

    # Events (cockpit_events / EventsPane)
    event_decision_exec: str
    event_risk_reject: str
    event_stale: str
    event_hold: str
    event_watch: str
    event_learning: str
    event_cycle: str
    event_error: str
    event_other: str


# ---------------------------------------------------------------------------
# PALETTE_DARK — couleurs actuelles de tui.py (fond sombre) — NE PAS MODIFIER
# ---------------------------------------------------------------------------
PALETTE_DARK: Palette = {
    "border_default": "cyan",
    "border_attribution": "magenta",
    "border_learnings": "blue",
    "border_plans": "cyan",
    "border_watches": "cyan",
    "border_llm_activity": "magenta",
    "equity_line": "cyan",
    "equity_dim": "dim",
    "kpi_sharpe_ok": "green",
    "kpi_sharpe_bad": "red",
    "kpi_default": "cyan",
    "kpi_vol_warn": "yellow",
    "pnl_positive": "green",
    "pnl_negative": "red",
    "symbol_bold": "bold cyan",
    "action_buy": "green",
    "action_sell": "red",
    "action_hold": "dim",
    "learning_symbol": "bold cyan",
    "dim": "dim",
    "status_equity": "bold cyan",
    "status_accent": "cyan",
    "status_phase": "magenta",
    "status_nominal": "green",
    "event_decision_exec": "bold green",
    "event_risk_reject": "bold red",
    "event_stale": "dim",
    "event_hold": "dim white",
    "event_watch": "bold cyan",
    "event_learning": "blue",
    "event_cycle": "dim grey50",
    "event_error": "bold yellow",
    "event_other": "dim",
}

# ---------------------------------------------------------------------------
# PALETTE_LIGHT — déclinaison saumon FT editorial
#
# Accent structurel : bleu pétrole #0D7680 (bordures, titres, accents).
# Vert #1A6B2F réservé exclusivement aux signaux haussiers (BUY, gains).
# Rouge carmin #B52A2A pour signaux baissiers (SELL, pertes, risk-reject).
# Contraste #0D7680 sur #FFF1E5 : ratio ≈ 5.2:1 (AA/AAA conforme).
# ---------------------------------------------------------------------------
PALETTE_LIGHT: Palette = {
    "border_default": "#0D7680",
    "border_attribution": "#0D7680",
    "border_learnings": "#0D7680",
    "border_plans": "#0D7680",
    "border_watches": "#0D7680",
    "border_llm_activity": "#0D7680",
    "equity_line": "#0D7680",
    "equity_dim": "#6B6057 dim",
    "kpi_sharpe_ok": "#1A6B2F",
    "kpi_sharpe_bad": "#B52A2A",
    "kpi_default": "#0D7680",
    "kpi_vol_warn": "#C47B00",
    "pnl_positive": "#1A6B2F",
    "pnl_negative": "#B52A2A",
    "symbol_bold": "bold #0D7680",
    "action_buy": "#1A6B2F",
    "action_sell": "#B52A2A",
    "action_hold": "#6B6057 dim",
    "learning_symbol": "bold #0D7680",
    "dim": "#6B6057 dim",
    "status_equity": "bold #0D7680",
    "status_accent": "#0D7680",
    "status_phase": "#0D7680",
    "status_nominal": "#1A6B2F",
    "event_decision_exec": "bold #1A6B2F",
    "event_risk_reject": "bold #B52A2A",
    "event_stale": "#6B6057 dim",
    "event_hold": "#6B6057",
    "event_watch": "bold #0D7680",
    "event_learning": "#0D7680",
    "event_cycle": "#B5A89A dim",
    "event_error": "bold #C47B00",
    "event_other": "#6B6057 dim",
}

# ---------------------------------------------------------------------------
# PALETTE_INK — sombre gruvbox (cockpit « mode Gonzo », thème par défaut).
# PALETTE_DARK reste la palette historique de tui.py : NE PAS Y TOUCHER.
# ---------------------------------------------------------------------------
PALETTE_INK: Palette = {
    "border_default": "#665c54",
    "border_attribution": "#665c54",
    "border_learnings": "#665c54",
    "border_plans": "#665c54",
    "border_watches": "#665c54",
    "border_llm_activity": "#665c54",
    "equity_line": "#8ec07c",
    "equity_dim": "#928374",
    "kpi_sharpe_ok": "#b8bb26",
    "kpi_sharpe_bad": "#fb4934",
    "kpi_default": "#83a598",
    "kpi_vol_warn": "#d79921",
    "pnl_positive": "#b8bb26",
    "pnl_negative": "#fb4934",
    "symbol_bold": "bold #83a598",
    "action_buy": "#b8bb26",
    "action_sell": "#fb4934",
    "action_hold": "#928374",
    "learning_symbol": "bold #83a598",
    "dim": "#928374",
    "status_equity": "bold #ebdbb2",
    "status_accent": "#83a598",
    "status_phase": "#d3869b",
    "status_nominal": "#b8bb26",
    "event_decision_exec": "bold #b8bb26",
    "event_risk_reject": "bold #fb4934",
    "event_stale": "#928374",
    "event_hold": "#a89984",
    "event_watch": "bold #8ec07c",
    "event_learning": "#83a598",
    "event_cycle": "#7c6f64",
    "event_error": "bold #d79921",
    "event_other": "#928374",
}

# Ensemble canonique de toutes les clés valides
ALL_PALETTE_KEYS: tuple[str, ...] = tuple(PALETTE_DARK.keys())
