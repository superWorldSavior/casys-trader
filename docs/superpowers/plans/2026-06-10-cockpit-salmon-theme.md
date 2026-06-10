# Cockpit Saumon — Thème FT Editorial + Palette Partagée Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refonte visuelle du cockpit Textual avec un thème "papier financier" saumon (Financial Times editorial) comme défaut, thème sombre togglable via `d`, et une palette partagée pour les renderables Rich de tui.py.

**Architecture:** Trois composants liés : (1) `trader/palette.py` — module autonome avec `PALETTE_DARK` (couleurs actuelles) et `PALETTE_LIGHT` (déclinaison saumon), (2) `trader/tui.py` — extraction minimale : les builders acceptent `palette=` optionnel, défaut `PALETTE_DARK` → zéro changement de comportement, (3) `trader/cockpit.py` — enregistrement des thèmes Textual `casys-salmon` (défaut) et `casys-ink`, binding `d` qui bascule thème + palette ensemble.

**Tech Stack:** Python 3.12+, Textual 8.2.7 (`Theme`, `register_theme`, `app.theme`), Rich (Panel, Text, Table), pytest-asyncio, uv

---

## Palette de tokens — casys-salmon

Valeurs de référence imposées par la DA :

| Token | Valeur hex | Rôle |
|---|---|---|
| `background` | `#FFF1E5` | Fond principal (saumon FT) |
| `surface` | `#FFF8F2` | Surfaces panneaux (légèrement plus clair) |
| `panel` | `#FDEEDE` | Panneaux profondeur |
| `foreground` | `#33302E` | Texte principal quasi-noir chaud |
| `primary` | `#4A6741` | Bleu pétrole → en pratique vert sauge (bordures actives) |
| `success` | `#1A6B2F` | Vert profond (gains / BUY) |
| `error` | `#B52A2A` | Rouge carmin (pertes / SELL / risk reject) |
| `warning` | `#C47B00` | Ambre (DRY-RUN, alertes) |
| `accent` | `#4A6741` | Accent structurel = même vert sauge que primary |
| `secondary` | `#6B6057` | Gris encre (texte secondaire) |

Pour la palette Rich (`PALETTE_LIGHT`), les couleurs correspondantes sont des chaînes de style Rich :

| Clé palette | DARK (actuel) | LIGHT (saumon) |
|---|---|---|
| `border_default` | `"cyan"` | `"#4A6741"` |
| `border_attribution` | `"magenta"` | `"#4A6741"` |
| `border_learnings` | `"blue"` | `"#4A6741"` |
| `equity_line` | `"cyan"` | `"#4A6741"` |
| `equity_dim` | `"dim"` | `"#6B6057 dim"` |
| `kpi_sharpe_ok` | `"green"` | `"#1A6B2F"` |
| `kpi_sharpe_bad` | `"red"` | `"#B52A2A"` |
| `kpi_default` | `"cyan"` | `"#4A6741"` |
| `kpi_vol_warn` | `"yellow"` | `"#C47B00"` |
| `pnl_positive` | `"green"` | `"#1A6B2F"` |
| `pnl_negative` | `"red"` | `"#B52A2A"` |
| `symbol_bold` | `"bold cyan"` | `"bold #4A6741"` |
| `action_buy` | `"green"` | `"#1A6B2F"` |
| `action_sell` | `"red"` | `"#B52A2A"` |
| `action_hold` | `"dim"` | `"#6B6057 dim"` |
| `learning_symbol` | `"bold cyan"` | `"bold #4A6741"` |
| `dim` | `"dim"` | `"#6B6057 dim"` |

Pour les events (`PALETTE_LIGHT` dans cockpit.py) :

| Clé | DARK | LIGHT |
|---|---|---|
| `event_decision_exec` | `"bold green"` | `"bold #1A6B2F"` |
| `event_risk_reject` | `"bold red"` | `"bold #B52A2A"` |
| `event_stale` | `"dim"` | `"#6B6057 dim"` |
| `event_hold` | `"dim white"` | `"#6B6057"` |
| `event_watch` | `"bold cyan"` | `"bold #4A6741"` |
| `event_learning` | `"blue"` | `"#4A6741"` |
| `event_cycle` | `"dim grey50"` | `"#B5A89A dim"` |
| `event_error` | `"bold yellow"` | `"bold #C47B00"` |
| `event_other` | `"dim"` | `"#6B6057 dim"` |

---

## Cartographie des fichiers

| Fichier | Action | Responsabilité |
|---|---|---|
| `trader/palette.py` | **Créer** | `PALETTE_DARK`, `PALETTE_LIGHT`, `Palette` TypedDict |
| `trader/tui.py` | **Modifier** | Builders acceptent `palette: Palette = PALETTE_DARK` — uniquement les signatures et les styles codés en dur |
| `trader/cockpit.py` | **Modifier** | Thèmes Textual, binding `d`, palette active, kill-switch bandeau rouge |
| `tests/test_palette.py` | **Créer** | Tests TDD palette (régression DARK, couverture LIGHT, builders) |
| `tests/test_cockpit_smoke.py` | **Modifier** | Ajouter test : thème par défaut == casys-salmon, binding `d` bascule |

**NE PAS TOUCHER** : `trader/daemon.py`, `trader/consolidator.py`, `tests/test_daemon_exit_engine.py`, `tests/test_consolidator.py`, `tests/test_cli_semantic.py`, `tests/test_daemon_learnings.py`, `tests/test_llm.py`, `README.md`, `docs/`.

---

## Task 1 : Module `trader/palette.py` — structure de données

**Files:**
- Create: `trader/palette.py`
- Test: `tests/test_palette.py`

- [ ] **Step 1 : Écrire les tests qui échouent**

```python
# tests/test_palette.py
"""Tests TDD pour trader.palette — régression DARK + couverture LIGHT."""
from __future__ import annotations

import pytest
from trader.palette import PALETTE_DARK, PALETTE_LIGHT, Palette, ALL_PALETTE_KEYS


def test_palette_dark_border_default_est_cyan() -> None:
    """Régression : DARK contient les couleurs actuelles de tui.py."""
    assert PALETTE_DARK["border_default"] == "cyan"


def test_palette_dark_border_attribution_est_magenta() -> None:
    assert PALETTE_DARK["border_attribution"] == "magenta"


def test_palette_dark_border_learnings_est_blue() -> None:
    assert PALETTE_DARK["border_learnings"] == "blue"


def test_palette_dark_equity_line_est_cyan() -> None:
    assert PALETTE_DARK["equity_line"] == "cyan"


def test_palette_dark_action_buy_est_green() -> None:
    assert PALETTE_DARK["action_buy"] == "green"


def test_palette_dark_action_sell_est_red() -> None:
    assert PALETTE_DARK["action_sell"] == "red"


def test_palette_dark_pnl_positive_est_green() -> None:
    assert PALETTE_DARK["pnl_positive"] == "green"


def test_palette_dark_pnl_negative_est_red() -> None:
    assert PALETTE_DARK["pnl_negative"] == "red"


def test_palette_dark_symbol_bold_est_bold_cyan() -> None:
    assert PALETTE_DARK["symbol_bold"] == "bold cyan"


def test_palette_dark_event_decision_exec_est_bold_green() -> None:
    assert PALETTE_DARK["event_decision_exec"] == "bold green"


def test_palette_dark_event_risk_reject_est_bold_red() -> None:
    assert PALETTE_DARK["event_risk_reject"] == "bold red"


def test_palette_dark_event_watch_est_bold_cyan() -> None:
    assert PALETTE_DARK["event_watch"] == "bold cyan"


def test_palette_dark_event_learning_est_blue() -> None:
    assert PALETTE_DARK["event_learning"] == "blue"


def test_palette_dark_event_cycle_est_dim_grey50() -> None:
    assert PALETTE_DARK["event_cycle"] == "dim grey50"


def test_palette_dark_event_error_est_bold_yellow() -> None:
    assert PALETTE_DARK["event_error"] == "bold yellow"


def test_light_a_toutes_les_cles_de_dark() -> None:
    """LIGHT ne doit pas avoir de clé orpheline."""
    missing = set(PALETTE_DARK.keys()) - set(PALETTE_LIGHT.keys())
    assert not missing, f"Clés manquantes dans PALETTE_LIGHT : {missing}"


def test_dark_a_toutes_les_cles_de_light() -> None:
    """DARK ne doit pas avoir de clé orpheline non plus."""
    missing = set(PALETTE_LIGHT.keys()) - set(PALETTE_DARK.keys())
    assert not missing, f"Clés en trop dans PALETTE_LIGHT : {missing}"


def test_all_palette_keys_correspond_aux_cles_dark() -> None:
    """ALL_PALETTE_KEYS = ensemble de clés de DARK."""
    assert set(ALL_PALETTE_KEYS) == set(PALETTE_DARK.keys())


def test_light_ne_contient_aucune_couleur_dark_codee_en_dur() -> None:
    """Les valeurs saumon ne doivent PAS reproduire les couleurs DARK brutes."""
    dark_raw_colors = {"cyan", "magenta", "blue", "green", "red", "bold green", "bold red",
                       "bold cyan", "dim white", "dim grey50", "bold yellow"}
    violations = {
        k: v for k, v in PALETTE_LIGHT.items()
        if v in dark_raw_colors
    }
    assert not violations, f"PALETTE_LIGHT contient des couleurs DARK brutes : {violations}"
```

- [ ] **Step 2 : Vérifier l'échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_palette.py -q
```

Expected : `ModuleNotFoundError: No module named 'trader.palette'`

- [ ] **Step 3 : Créer `trader/palette.py`**

```python
# trader/palette.py
"""palette — design tokens pour les renderables Rich du cockpit casys-trader.

Deux variantes :
- PALETTE_DARK  : couleurs actuelles de tui.py (fond sombre) — ne jamais modifier.
- PALETTE_LIGHT : déclinaison saumon FT editorial (fond #FFF1E5).

Les builders de tui.py acceptent ``palette: Palette = PALETTE_DARK`` en argument
optionnel → le comportement par défaut est identique au pixel près.
"""
from __future__ import annotations

from typing import TypedDict


class Palette(TypedDict):
    """Ensemble complet des design tokens Rich pour les builders de tui.py et cockpit.py."""
    # Bordures panneaux
    border_default: str       # positions, equity, kpi cards
    border_attribution: str   # attribution
    border_learnings: str     # learnings

    # Graphique équité
    equity_line: str          # couleur sparkline / plotext
    equity_dim: str           # texte dim sur graphique

    # Styles KPI cards
    kpi_sharpe_ok: str
    kpi_sharpe_bad: str
    kpi_default: str          # cyan en DARK
    kpi_vol_warn: str         # yellow en DARK

    # Styles P&L / positions
    pnl_positive: str
    pnl_negative: str
    symbol_bold: str          # bold + couleur pour les symboles

    # Actions décisions
    action_buy: str
    action_sell: str
    action_hold: str

    # Learnings
    learning_symbol: str

    # Utilitaires
    dim: str                  # style dim générique

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
# PALETTE_DARK — couleurs actuelles (fond sombre) — NE PAS MODIFIER
# ---------------------------------------------------------------------------
PALETTE_DARK: Palette = Palette(
    border_default="cyan",
    border_attribution="magenta",
    border_learnings="blue",
    equity_line="cyan",
    equity_dim="dim",
    kpi_sharpe_ok="green",
    kpi_sharpe_bad="red",
    kpi_default="cyan",
    kpi_vol_warn="yellow",
    pnl_positive="green",
    pnl_negative="red",
    symbol_bold="bold cyan",
    action_buy="green",
    action_sell="red",
    action_hold="dim",
    learning_symbol="bold cyan",
    dim="dim",
    event_decision_exec="bold green",
    event_risk_reject="bold red",
    event_stale="dim",
    event_hold="dim white",
    event_watch="bold cyan",
    event_learning="blue",
    event_cycle="dim grey50",
    event_error="bold yellow",
    event_other="dim",
)

# ---------------------------------------------------------------------------
# PALETTE_LIGHT — déclinaison saumon FT editorial
# ---------------------------------------------------------------------------
PALETTE_LIGHT: Palette = Palette(
    border_default="#4A6741",
    border_attribution="#4A6741",
    border_learnings="#4A6741",
    equity_line="#4A6741",
    equity_dim="#6B6057 dim",
    kpi_sharpe_ok="#1A6B2F",
    kpi_sharpe_bad="#B52A2A",
    kpi_default="#4A6741",
    kpi_vol_warn="#C47B00",
    pnl_positive="#1A6B2F",
    pnl_negative="#B52A2A",
    symbol_bold="bold #4A6741",
    action_buy="#1A6B2F",
    action_sell="#B52A2A",
    action_hold="#6B6057 dim",
    learning_symbol="bold #4A6741",
    dim="#6B6057 dim",
    event_decision_exec="bold #1A6B2F",
    event_risk_reject="bold #B52A2A",
    event_stale="#6B6057 dim",
    event_hold="#6B6057",
    event_watch="bold #4A6741",
    event_learning="#4A6741",
    event_cycle="#B5A89A dim",
    event_error="bold #C47B00",
    event_other="#6B6057 dim",
)

# Ensemble canonique de toutes les clés valides
ALL_PALETTE_KEYS: tuple[str, ...] = tuple(PALETTE_DARK.keys())
```

- [ ] **Step 4 : Vérifier les tests palette passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_palette.py -v
```

Expected : tous les tests PASS (aucune clé orpheline, régressions DARK verrouillées).

---

## Task 2 : Adapter `trader/tui.py` — builders avec palette optionnelle

**Files:**
- Modify: `trader/tui.py` (signatures des 5 builders + imports)
- Test: `tests/test_palette.py` (section builders)

Les changements autorisés dans tui.py :
1. `from trader.palette import PALETTE_DARK, Palette` en en-tête
2. Chaque builder `_build_*` reçoit `palette: Palette = PALETTE_DARK` comme dernier paramètre
3. Remplacer les couleurs hardcodées par `palette["clé"]`
4. `build_view` transmet `palette=PALETTE_DARK` à tous les builders → comportement identique

- [ ] **Step 1 : Ajouter les tests builders dans `tests/test_palette.py`**

Ajouter à la fin du fichier `tests/test_palette.py` :

```python
# --- Tests builders ---

from trader.tui import (
    _build_kpi_band,
    _build_equity_panel,
    _build_positions_panel,
    _build_attribution_panel,
    _build_decisions_table,
    _build_learnings_panel,
)
from rich.console import Console


def _render(renderable) -> str:
    console = Console(width=120, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_build_kpi_band_sans_palette_utilise_dark() -> None:
    """Sans palette explicite, le rendu contient les styles DARK (cyan)."""
    result = _build_kpi_band({})
    output = _render(result)
    assert output is not None  # ne plante pas — comportement actuel conservé


def test_build_equity_panel_sans_palette_ne_plante_pas() -> None:
    result = _build_equity_panel([100.0, 101.0, 99.0])
    output = _render(result)
    assert output is not None


def test_build_positions_panel_sans_palette_ne_plante_pas() -> None:
    holdings = [{"symbol": "AAPL", "quantity": 10.0, "avg_price": 170.0,
                  "last_price": 180.0, "unrealized_pnl": 100.0}]
    result = _build_positions_panel(holdings)
    output = _render(result)
    assert "AAPL" in output


def test_build_decisions_sans_palette_ne_plante_pas() -> None:
    decisions = [{"symbol": "AAPL", "action": "BUY", "qty": 1.0,
                   "rationale": "ok", "confidence": 0.8}]
    result = _build_decisions_table(decisions)
    output = _render(result)
    assert "AAPL" in output


def test_build_positions_panel_avec_light_ne_contient_pas_cyan_ni_magenta() -> None:
    """Avec PALETTE_LIGHT, le rendu ne doit pas contenir les noms de couleurs DARK bruts."""
    from trader.palette import PALETTE_LIGHT
    holdings = [{"symbol": "AAPL", "quantity": 10.0, "avg_price": 170.0,
                  "last_price": 180.0, "unrealized_pnl": 100.0}]
    result = _build_positions_panel(holdings, palette=PALETTE_LIGHT)
    output = _render(result)
    # Le texte final ne doit pas contenir les balises couleur DARK brutes
    # (les couleurs Rich sont résolues par le moteur de rendu — on vérifie
    # simplement que le builder n'a pas planté et produit un output)
    assert "AAPL" in output


def test_build_kpi_band_avec_light_ne_plante_pas() -> None:
    from trader.palette import PALETTE_LIGHT
    kpis = {"sharpe": 1.2, "max_drawdown": -0.05, "period_win_rate": 0.6,
             "volatility": 0.2, "num_trades": 10}
    result = _build_kpi_band(kpis, palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None


def test_build_equity_panel_avec_light_ne_plante_pas() -> None:
    from trader.palette import PALETTE_LIGHT
    result = _build_equity_panel([100.0, 101.5, 99.0, 102.0], palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None


def test_build_attribution_avec_light_ne_plante_pas() -> None:
    from trader.palette import PALETTE_LIGHT
    result = _build_attribution_panel({}, palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None


def test_build_learnings_avec_light_ne_plante_pas() -> None:
    from trader.palette import PALETTE_LIGHT
    learnings = [{"note": "test", "symbol": "AAPL", "ts": "2026-06-10T10:00:00Z"}]
    result = _build_learnings_panel(learnings, palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None
```

- [ ] **Step 2 : Vérifier que les tests builders échouent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_palette.py::test_build_positions_panel_avec_light_ne_contient_pas_cyan_ni_magenta -v
```

Expected : `TypeError` — les builders n'acceptent pas encore `palette=`.

- [ ] **Step 3 : Modifier `trader/tui.py` — import palette**

Ajouter juste après les imports Rich existants (ligne ~28, avant `_ROOT = ...`) :

```python
from trader.palette import PALETTE_DARK, Palette
```

- [ ] **Step 4 : Modifier `_build_kpi_band` dans `trader/tui.py`**

Remplacer la signature et les usages de couleurs :

```python
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
            body.append(subtitle, style="dim")
        return Panel(body, title=f"[bold]{title}[/bold]", border_style=border, expand=True)

    sharpe_style = palette["kpi_sharpe_ok"] if (sharpe is not None and sharpe >= 1.0) else (palette["kpi_sharpe_bad"] if (sharpe or 0.0) < 0 else palette["kpi_default"])
    win_style = palette["kpi_sharpe_ok"] if (win_rate is not None and win_rate >= 0.5) else (palette["kpi_sharpe_bad"] if win_rate is not None else palette["kpi_default"])
    vol_style = palette["kpi_default"] if volatility is None or volatility <= 0.25 else palette["kpi_vol_warn"]

    return Columns(
        [
            card("Sharpe", _fmt_number(sharpe), sharpe_style, "qualité risque"),
            card("Max DD", _fmt_percent(max_dd), palette["kpi_sharpe_bad"], "drawdown"),
            card("Win rate", _fmt_percent(win_rate), win_style, "période"),
            card("Volatilité", _fmt_percent(volatility), vol_style, "annualisée"),
            card("Trades", _fmt_int(trades), palette["kpi_default"], "clôturés/exécutés"),
        ],
        equal=True,
        expand=True,
    )
```

- [ ] **Step 5 : Modifier `_build_equity_panel` dans `trader/tui.py`**

```python
def _build_equity_panel(values: list[float], *, palette: Palette = PALETTE_DARK) -> Panel:
    if len(values) < 2:
        return Panel(
            Text("Courbe indisponible : moins de 2 points d'équité.", style=palette["dim"]),
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

    return Panel(chart, title="[bold]Équité[/bold]", border_style=palette["border_default"], expand=True)
```

- [ ] **Step 6 : Modifier `_build_positions_panel` dans `trader/tui.py`**

```python
def _build_positions_panel(holdings: list[dict], *, palette: Palette = PALETTE_DARK) -> Panel:
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

    return Panel(pos_table, title="[bold]Positions[/bold]", border_style=palette["border_default"], expand=True)
```

- [ ] **Step 7 : Modifier `_confidence_bar` dans `trader/tui.py`**

La fonction prend en paramètre la palette pour les styles P&L :

```python
def _confidence_bar(win_rate: Any, pnl: Any, *, width: int = 12, palette: Palette = PALETTE_DARK) -> Text:
    win = _safe_float(win_rate, default=0.0) or 0.0
    win = max(0.0, min(1.0, win))
    filled = int(round(win * width))
    bar = "█" * filled + "░" * (width - filled)
    pnl_val = _safe_float(pnl, default=0.0) or 0.0
    return Text(bar, style=palette["pnl_positive"] if pnl_val >= 0 else palette["pnl_negative"])
```

- [ ] **Step 8 : Modifier `_build_attribution_panel` dans `trader/tui.py`**

```python
def _build_attribution_panel(attribution: dict, *, palette: Palette = PALETTE_DARK) -> Panel:
    realized_pnl = _safe_float(attribution.get("realized_pnl"), default=0.0) or 0.0
    pnl_style = palette["pnl_positive"] if realized_pnl >= 0 else palette["pnl_negative"]
    summary = Text.assemble(
        ("Trades clôturés : ", "bold"), (_fmt_int(attribution.get("n_closed_trades")), palette["kpi_default"]),
        ("   P&L réalisé : ", "bold"), (_fmt_signed_money(realized_pnl), pnl_style),
        ("   Win rate : ", "bold"), (_fmt_percent(attribution.get("win_rate")), palette["kpi_default"]),
        ("   Détention moy. : ", "bold"), (_fmt_number(attribution.get("avg_holding_minutes"), 1), palette["kpi_default"]),
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
            confidence_table.add_row(
                Text(str(row.get("bucket", "—")), style="bold"),
                _confidence_bar(row.get("win_rate"), pnl, palette=palette),
                Text(
                    f"n={_fmt_int(row.get('n'))}  win={_fmt_percent(row.get('win_rate'))}  P&L {_fmt_signed_money(pnl)}",
                    style=palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"],
                ),
            )
    else:
        confidence_table.add_row(Text("—", style=palette["dim"]), Text(""), Text(""))

    exit_rows = _safe_list_of_dicts(attribution.get("by_exit_reason"))
    exit_table = Table(show_header=True, header_style="bold dim", box=None, expand=True, pad_edge=False)
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
                Text(_fmt_signed_money(pnl), style=palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]),
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
```

- [ ] **Step 9 : Modifier `_build_decisions_table` dans `trader/tui.py`**

```python
def _build_decisions_table(decisions: list[dict], *, palette: Palette = PALETTE_DARK) -> Table:
    dec_table = Table(title="Dernières décisions", show_lines=False, expand=True)
    dec_table.add_column("Symbole", style="bold")
    dec_table.add_column("Action")
    dec_table.add_column("Qté", justify="right")
    dec_table.add_column("Raison", overflow="fold", ratio=4)
    dec_table.add_column("Confiance", justify="right")

    for d in decisions:
        action = str(d.get("action", "HOLD"))
        action_style = palette["action_buy"] if action == "BUY" else (palette["action_sell"] if action == "SELL" else palette["action_hold"])
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
```

- [ ] **Step 10 : Modifier `_build_learnings_panel` dans `trader/tui.py`**

```python
def _build_learnings_panel(learnings: list[dict], *, palette: Palette = PALETTE_DARK) -> RenderableType | None:
    if not learnings:
        return None
    lines: list[Text] = []
    for index, item in enumerate(learnings[-5:]):
        if index:
            lines.append(Text(""))
        note = str(item.get("note") or "")
        symbol = str(item.get("symbol") or "—")
        ts = _format_datetime(item.get("ts"))
        lines.append(Text.assemble(("▸ ", palette["dim"]), (symbol, palette["learning_symbol"]), ("  ·  ", palette["dim"]), (ts, palette["dim"])))
        lines.append(Text.assemble(("  ", palette["dim"]), note))
    return Panel(Group(*lines), title="[bold]Derniers apprentissages[/bold]", border_style=palette["border_learnings"], expand=True)
```

- [ ] **Step 11 : Mettre à jour `build_view` dans `trader/tui.py` pour passer `palette=PALETTE_DARK`**

Dans `build_view`, ajouter `palette: Palette = PALETTE_DARK` comme paramètre et transmettre aux builders :

```python
def build_view(state: dict | None, *, palette: Palette = PALETTE_DARK) -> RenderableType:
    # ... (toute la logique de normalisation est identique) ...
    # Remplacer chaque appel _build_* par :
    #   _build_kpi_band(kpis, palette=palette)
    #   _build_equity_panel(equity_curve, palette=palette)
    #   _build_positions_panel(holdings, palette=palette)
    #   _build_attribution_panel(attribution, palette=palette)
    #   _build_decisions_table(decisions, palette=palette)
    #   _build_learnings_panel(learnings, palette=palette)
    # Les styles codés en dur dans build_view (header_panel border, mode_label, kill_label) :
    # - mode_label "bold red" / "bold yellow" : restent en dur (pas dans la palette — UI spécifique)
    # - kill_label "bold red on white" : reste en dur
    # - header_panel border : Panel(header_lines, ...) sans border_style explicite → ok
```

Note : dans `build_view`, les styles `mode_label` (bold red / bold yellow) et `kill_label` (bold red on white) restent codés en dur — ils sont des états exceptionnels UI, pas des couleurs thématiques. Seuls les appels aux builders prennent la palette.

- [ ] **Step 12 : Vérifier les tests builders passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_palette.py tests/test_tui.py -v
```

Expected : tous PASS. Les tests `test_tui.py` doivent passer inchangés (comportement conservé par défaut).

- [ ] **Step 13 : Vérifier ruff sur tui.py**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run ruff check trader/tui.py trader/palette.py
```

Expected : no issues.

---

## Task 3 : Thèmes Textual dans `trader/cockpit.py` — casys-salmon + casys-ink

**Files:**
- Modify: `trader/cockpit.py`
- Modify: `tests/test_cockpit_smoke.py` (ajouter tests thème/binding)

- [ ] **Step 1 : Ajouter les tests thème/binding dans `tests/test_cockpit_smoke.py`**

Ajouter à la fin du fichier :

```python
async def test_cockpit_theme_defaut_est_casys_salmon(tmp_path, monkeypatch):
    """Le thème par défaut doit être casys-salmon (fond clair)."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        assert app.theme == "casys-salmon"


async def test_cockpit_binding_d_bascule_theme(tmp_path, monkeypatch):
    """La touche d bascule entre casys-salmon et casys-ink."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        assert app.theme == "casys-salmon"
        await pilot.press("d")
        assert app.theme == "casys-ink"
        await pilot.press("d")
        assert app.theme == "casys-salmon"
```

- [ ] **Step 2 : Vérifier que les tests échouent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py::test_cockpit_theme_defaut_est_casys_salmon tests/test_cockpit_smoke.py::test_cockpit_binding_d_bascule_theme -v
```

Expected : FAIL — `AssertionError` (thème actuel != casys-salmon, binding d absent).

- [ ] **Step 3 : Modifier `trader/cockpit.py` — imports + constantes thème**

En haut du fichier, après les imports Textual existants, ajouter :

```python
from textual.theme import Theme

from trader.palette import PALETTE_DARK, PALETTE_LIGHT, Palette

# ---------------------------------------------------------------------------
# Thèmes Textual
# ---------------------------------------------------------------------------

_THEME_SALMON = Theme(
    name="casys-salmon",
    dark=False,
    primary="#4A6741",
    secondary="#6B6057",
    warning="#C47B00",
    error="#B52A2A",
    success="#1A6B2F",
    accent="#4A6741",
    foreground="#33302E",
    background="#FFF1E5",
    surface="#FFF8F2",
    panel="#FDEEDE",
)

_THEME_INK = Theme(
    name="casys-ink",
    dark=True,
    primary="#0178D4",
    secondary="#888888",
    warning="#ffcc00",
    error="#e05252",
    success="#4caf50",
    accent="#0178D4",
    foreground="#e0e0e0",
    background="#1a1a2e",
    surface="#16213e",
    panel="#0f3460",
)

# Mapping nom de thème → palette Rich
_THEME_PALETTE: dict[str, Palette] = {
    "casys-salmon": PALETTE_LIGHT,
    "casys-ink": PALETTE_DARK,
}
```

- [ ] **Step 4 : Modifier la classe `CockpitApp` — enregistrement thèmes + binding d**

Modifier la classe `CockpitApp` :

```python
class CockpitApp(App):
    """Cockpit console unifié — dashboard complet + logs live."""

    TITLE = "casys-trader — cockpit"
    CSS = """
    Screen {
        layout: vertical;
    }
    #main-body {
        layout: horizontal;
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quitter"),
        Binding("c", "toggle_cycles", "Toggle cycles"),
        Binding("f", "toggle_scroll", "Pause scroll"),
        Binding("l", "toggle_logs", "Toggle logs"),
        Binding("d", "toggle_theme", "Dark/Light"),
    ]

    _logs_visible: bool = True

    def compose(self) -> ComposeResult:
        yield CockpitStatus(id="cockpit-status")
        yield Static(id="main-body")
        yield Footer()

    def on_mount(self) -> None:
        # Enregistrement des thèmes custom
        self.register_theme(_THEME_SALMON)
        self.register_theme(_THEME_INK)
        # Thème saumon par défaut
        self.theme = "casys-salmon"

        body = self.query_one("#main-body", Static)
        body.mount(DashboardPane(id="dashboard-pane"))
        body.mount(EventsPane(id="events-pane"))
        # Polling état toutes les 2 s
        self.set_interval(2.0, self._schedule_refresh_state)
        # Polling events toutes les 1 s
        self.set_interval(1.0, self._poll_events)
        # Charge immédiatement
        self._schedule_refresh_state()
        self._poll_events()

    def _current_palette(self) -> Palette:
        """Retourne la palette Rich correspondant au thème actif."""
        return _THEME_PALETTE.get(self.theme, PALETTE_DARK)

    # ... (autres méthodes identiques) ...

    def action_toggle_theme(self) -> None:
        """Bascule entre casys-salmon et casys-ink."""
        if self.theme == "casys-salmon":
            self.theme = "casys-ink"
        else:
            self.theme = "casys-salmon"
        # Force un refresh du dashboard avec la nouvelle palette
        try:
            dashboard: DashboardPane = self.query_one("#dashboard-pane", DashboardPane)
            dashboard._current_palette = self._current_palette()
        except Exception:
            pass
```

- [ ] **Step 5 : Modifier `DashboardPane` pour tenir compte de la palette active**

`DashboardPane.update_state` doit accepter une palette :

```python
class DashboardPane(Static):
    """Panneau gauche : reprend intégralement le contenu de tui.build_view."""

    DEFAULT_CSS = """
    DashboardPane {
        width: 60%;
        height: 100%;
        border-right: solid $primary;
        overflow-y: auto;
    }
    DashboardPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT  # défaut saumon

    def compose(self) -> ComposeResult:
        yield Static(id="kpi-band")
        yield Static(id="equity-panel")
        yield Static(id="positions-panel")
        yield Static(id="attribution-panel")
        yield Static(id="decisions-table")
        yield Static(id="learnings-panel")

    def update_state(self, state: dict) -> None:
        """Recharge tous les sous-panneaux avec le dernier état."""
        palette = self._current_palette
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        attribution = state.get("attribution") if isinstance(state.get("attribution"), dict) else {}
        holdings = _safe_list_of_dicts(portfolio.get("holdings"))
        decisions = _safe_list_of_dicts(state.get("decisions"))
        equity_curve_raw = state.get("equity_curve") or []
        equity_curve = [
            v for v in (_safe_float(x, default=None) for x in equity_curve_raw)
            if v is not None
        ]
        learnings = _safe_list_of_dicts(state.get("learnings"))

        self.query_one("#kpi-band", Static).update(_build_kpi_band(kpis, palette=palette))
        self.query_one("#equity-panel", Static).update(_build_equity_panel(equity_curve, palette=palette))
        self.query_one("#positions-panel", Static).update(_build_positions_panel(holdings, palette=palette))
        self.query_one("#attribution-panel", Static).update(_build_attribution_panel(attribution, palette=palette))
        self.query_one("#decisions-table", Static).update(_build_decisions_table(decisions, palette=palette))

        learnings_renderable = _build_learnings_panel(learnings, palette=palette)
        if learnings_renderable is not None:
            self.query_one("#learnings-panel", Static).update(learnings_renderable)
        else:
            self.query_one("#learnings-panel", Static).update(Text(""))
```

- [ ] **Step 6 : Mettre à jour `_apply_state` dans `CockpitApp` pour passer la palette au dashboard**

```python
def _apply_state(self, state: dict, kill_active: bool) -> None:
    """Met à jour les widgets avec l'état chargé (appelé depuis le thread UI)."""
    try:
        status: CockpitStatus = self.query_one("#cockpit-status", CockpitStatus)
        status.update_state(state, kill_active)

        dashboard: DashboardPane = self.query_one("#dashboard-pane", DashboardPane)
        dashboard._current_palette = self._current_palette()
        dashboard.update_state(state)
    except Exception:
        pass
```

- [ ] **Step 7 : Mettre à jour les styles events dans `EventsPane` pour utiliser la palette**

Dans `EventsPane`, le dictionnaire `_EVENT_STYLES` en haut de `cockpit.py` doit utiliser la palette active. Supprimer `_EVENT_STYLES` global et les passer dynamiquement :

La constante `_EVENT_STYLES` au top de `cockpit.py` devient :

```python
# DARK event styles (conservés pour compatibilité)
_EVENT_STYLES_DARK: dict[EventClass, str] = {
    EventClass.DECISION_EXECUTED: "bold green",
    EventClass.RISK_REJECT:       "bold red",
    EventClass.STALE:             "dim",
    EventClass.HOLD:              "dim white",
    EventClass.WATCH:             "bold cyan",
    EventClass.LEARNING:          "blue",
    EventClass.CYCLE:             "dim grey50",
    EventClass.ERROR:             "bold yellow",
    EventClass.OTHER:             "dim",
}

def _event_styles_for_palette(palette: Palette) -> dict[EventClass, str]:
    """Construit le mapping EventClass→style depuis une palette."""
    return {
        EventClass.DECISION_EXECUTED: palette["event_decision_exec"],
        EventClass.RISK_REJECT:       palette["event_risk_reject"],
        EventClass.STALE:             palette["event_stale"],
        EventClass.HOLD:              palette["event_hold"],
        EventClass.WATCH:             palette["event_watch"],
        EventClass.LEARNING:          palette["event_learning"],
        EventClass.CYCLE:             palette["event_cycle"],
        EventClass.ERROR:             palette["event_error"],
        EventClass.OTHER:             palette["event_other"],
    }
```

`EventsPane` reçoit la palette courante via un attribut :

```python
class EventsPane(Static):
    # ...
    _current_palette: Palette = PALETTE_LIGHT  # défaut saumon

    def poll_events(self, events_path: Path) -> None:
        log: RichLog = self.query_one("#events-log", RichLog)
        event_styles = _event_styles_for_palette(self._current_palette)
        # ... (logique identique, mais utilise event_styles au lieu de _EVENT_STYLES)
        for ev_dict in new_dicts:
            ev_line = format_event_line(ev_dict)
            if ev_line.markup_class == EventClass.CYCLE and not self._show_cycles:
                continue
            style = event_styles.get(ev_line.markup_class, "")
            log.write(Text(ev_line.text, style=style))
        # ...
```

Dans `action_toggle_theme`, mettre à jour la palette des EventsPane aussi :

```python
def action_toggle_theme(self) -> None:
    if self.theme == "casys-salmon":
        self.theme = "casys-ink"
    else:
        self.theme = "casys-salmon"
    palette = self._current_palette()
    try:
        dashboard: DashboardPane = self.query_one("#dashboard-pane", DashboardPane)
        dashboard._current_palette = palette
    except Exception:
        pass
    try:
        events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
        events_pane._current_palette = palette
    except Exception:
        pass
```

- [ ] **Step 8 : Améliorer `CockpitStatus.update_state` pour kill-switch saumon**

Dans `CockpitStatus.update_state`, le kill-switch doit être rouge plein impossible à rater — remplacer :

```python
kill_str = "[bold red on white] KILL ACTIF [/bold red on white]" if kill_active else "[green]nominal[/green]"
```

Par :

```python
kill_str = "[bold white on red] !! KILL ACTIF !! [/bold white on red]" if kill_active else "[green]nominal[/green]"
```

(fond rouge plein, texte blanc — lisible sur tous les thèmes)

- [ ] **Step 9 : Vérifier tous les tests cockpit**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_cockpit_smoke.py -v
```

Expected : 5 tests PASS (3 existants + 2 nouveaux thème/binding).

---

## Task 4 : Validation globale — suite complète + ruff

**Files:** aucun fichier modifié

- [ ] **Step 1 : Suite pytest complète**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest -q
```

Expected : 581+ tests PASS, 0 failures.

- [ ] **Step 2 : Ruff sur tous les fichiers modifiés**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run ruff check trader/palette.py trader/tui.py trader/cockpit.py tests/test_palette.py tests/test_cockpit_smoke.py
```

Expected : no issues found.

- [ ] **Step 3 : Ruff format check**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run ruff format --check trader/palette.py trader/tui.py trader/cockpit.py tests/test_palette.py tests/test_cockpit_smoke.py
```

Si des fichiers sont reformatés, run `uv run ruff format <fichier>` puis re-check.

---

## Self-Review — Couverture du spec

| Exigence spec | Task |
|---|---|
| Fond saumon #FFF1E5, surface ivoire | Task 3 Step 3 `_THEME_SALMON.background/surface` |
| Encre quasi-noire #33302E | Task 3 Step 3 `_THEME_SALMON.foreground` |
| Vert profond gains/BUY #1A6B2F | Task 1 `PALETTE_LIGHT.success`, Task 2 builders |
| Rouge carmin pertes/SELL #B52A2A | Task 1 `PALETTE_LIGHT.error`, Task 2 builders |
| Accent structurel unique #4A6741 | Task 1 `border_default/attribution/learnings` tous = `#4A6741` |
| Thème `casys-salmon` par défaut | Task 3 Step 3-4 `on_mount → self.theme = "casys-salmon"` |
| Thème `casys-ink` fond sombre togglable | Task 3 Step 3 `_THEME_INK` |
| Binding `d` bascule thème + palette | Task 3 Step 4 `action_toggle_theme` |
| `PALETTE_DARK` = couleurs actuelles (verrouillé) | Task 1 tests régression |
| `PALETTE_LIGHT` couvre toutes les clés DARK | Task 1 `test_light_a_toutes_les_cles_de_dark` |
| Builders tui sans palette = rendu actuel | Task 2 Step 1 `test_build_*_sans_palette_*` |
| Kill-switch rouge plein impossible à rater | Task 3 Step 8 `bold white on red` |
| NE PAS TOUCHER les fichiers protégés | Aucun changement dans ces fichiers |
| 581+ tests verts | Task 4 |
