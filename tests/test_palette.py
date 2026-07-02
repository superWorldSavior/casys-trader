"""Tests TDD pour trader.ui.palette — régression DARK + couverture LIGHT."""

from __future__ import annotations

from rich.console import Console
from trader.ui.palette import ALL_PALETTE_KEYS, PALETTE_DARK, PALETTE_LIGHT
from trader.tui import (
    _build_attribution_panel,
    _build_decisions_table,
    _build_equity_panel,
    _build_kpi_band,
    _build_learnings_panel,
    _build_positions_panel,
)


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
    dark_raw_colors = {
        "cyan",
        "magenta",
        "blue",
        "green",
        "red",
        "bold green",
        "bold red",
        "bold cyan",
        "dim white",
        "dim grey50",
        "bold yellow",
    }
    violations = {k: v for k, v in PALETTE_LIGHT.items() if v in dark_raw_colors}
    assert not violations, (
        f"PALETTE_LIGHT contient des couleurs DARK brutes : {violations}"
    )


# ---------------------------------------------------------------------------
# Tests builders (ajoutés après implémentation Task 2)
# ---------------------------------------------------------------------------


def _render(renderable) -> str:
    console = Console(width=120, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _render_ansi(renderable) -> str:
    """Rendu avec codes ANSI — pour vérifier les couleurs réelles."""
    console = Console(
        width=120,
        highlight=False,
        force_terminal=True,
        color_system="256",
        no_color=False,
    )
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# ---------------------------------------------------------------------------
# Finding 5 — régressions styles DARK historiques (ex-tests trop faibles)
# ---------------------------------------------------------------------------


def test_build_positions_panel_dark_bordure_cyan() -> None:
    """Régression : la bordure du panneau Positions est cyan en DARK."""
    from rich.panel import Panel

    result = _build_positions_panel([])
    assert isinstance(result, Panel)
    assert result.border_style == "cyan"


def test_build_equity_panel_dark_bordure_cyan() -> None:
    """Régression : la bordure du panneau Équité est cyan en DARK."""
    from rich.panel import Panel

    result = _build_equity_panel([100.0, 101.0, 99.0])
    assert isinstance(result, Panel)
    assert result.border_style == "cyan"


def test_build_attribution_panel_dark_bordure_magenta() -> None:
    """Régression : la bordure du panneau Attribution est magenta en DARK."""
    from rich.panel import Panel

    result = _build_attribution_panel({})
    assert isinstance(result, Panel)
    assert result.border_style == "magenta"


def test_build_learnings_panel_dark_bordure_blue() -> None:
    """Régression : la bordure du panneau Learnings est blue en DARK."""
    from rich.panel import Panel

    learnings = [{"note": "test", "symbol": "X", "ts": "2026-01-01T00:00:00Z"}]
    result = _build_learnings_panel(learnings)
    assert isinstance(result, Panel)
    assert result.border_style == "blue"


def test_build_positions_pnl_positif_contient_vert_ansi() -> None:
    """Régression : PnL positif génère une couleur verte (ANSI 32) en DARK."""
    holdings = [
        {
            "symbol": "AAPL",
            "quantity": 10.0,
            "avg_price": 170.0,
            "last_price": 180.0,
            "unrealized_pnl": 100.0,
        }
    ]
    output = _render_ansi(_build_positions_panel(holdings))
    # Code ANSI vert = 32
    assert "32m" in output, "PnL positif doit générer du vert ANSI en DARK"


def test_build_positions_pnl_negatif_contient_rouge_ansi() -> None:
    """Régression : PnL négatif génère une couleur rouge (ANSI 31) en DARK."""
    holdings = [
        {
            "symbol": "TSLA",
            "quantity": 5.0,
            "avg_price": 250.0,
            "last_price": 200.0,
            "unrealized_pnl": -250.0,
        }
    ]
    output = _render_ansi(_build_positions_panel(holdings))
    assert "31m" in output, "PnL négatif doit générer du rouge ANSI en DARK"


def test_build_kpi_band_dark_contient_dim_ansi() -> None:
    """Régression : les subtitles KPI contiennent du dim (ANSI 2) en DARK.

    Finding 1 : vérifie que palette['dim'] (= 'dim' en DARK) est bien utilisé
    dans le subtitle des cartes KPI, pas un style en dur.
    """
    output = _render_ansi(_build_kpi_band({"sharpe": 0.5, "num_trades": 5}))
    assert "\x1b[2m" in output, "Subtitle KPI doit générer du dim ANSI en DARK"


def test_build_attribution_dark_contient_dim_ansi() -> None:
    """Régression : la table attribution vide contient du dim en DARK."""
    output = _render_ansi(_build_attribution_panel({}))
    assert "\x1b[2m" in output, "Attribution vide doit contenir du dim ANSI en DARK"


# ---------------------------------------------------------------------------
# Finding 1+2 — LIGHT ne produit pas de dim brut ANSI là où on attend dim
# ---------------------------------------------------------------------------


def test_build_kpi_band_light_subtitle_ne_contient_pas_dim_brut() -> None:
    """Finding 1 : avec PALETTE_LIGHT, le subtitle KPI doit passer par palette['dim']
    (#6B6057 dim) et NE DOIT PAS générer de dim brut ANSI \\x1b[2m dans les subtitles.

    Le dim brut sur fond saumon est peu lisible ; PALETTE_LIGHT['dim'] = '#6B6057 dim'
    donne un gris encre lisible.
    """
    import re

    kpis = {"sharpe": 0.5, "num_trades": 5}
    result = _build_kpi_band(kpis, palette=PALETTE_LIGHT)
    output = _render_ansi(result)
    # Chercher les positions de dim brut (\x1b[2m)
    # Avec palette['dim'] utilisé, les subtitles auront une couleur hex AVANT \x1b[2m
    # On vérifie qu'il n'y a plus de \x1b[2m *sans* couleur précédente (pattern: reset+dim)
    # Pattern caractéristique du dim brut après reset : \x1b[0m\x1b[2m
    bare_dim_after_reset = re.findall(r"\x1b\[0m\s*\x1b\[2m", output)
    assert not bare_dim_after_reset, (
        f"PALETTE_LIGHT ne doit pas générer de dim brut sans couleur : {bare_dim_after_reset[:3]}"
    )


def test_build_kpi_band_sans_palette_utilise_dark() -> None:
    """Finding 5 : sans palette explicite, comportement DARK conservé."""
    result = _build_kpi_band({})
    output = _render(result)
    assert output is not None


def test_build_equity_panel_sans_palette_ne_plante_pas() -> None:
    result = _build_equity_panel([100.0, 101.0, 99.0])
    output = _render(result)
    assert output is not None


def test_build_positions_panel_sans_palette_ne_plante_pas() -> None:
    holdings = [
        {
            "symbol": "AAPL",
            "quantity": 10.0,
            "avg_price": 170.0,
            "last_price": 180.0,
            "unrealized_pnl": 100.0,
        }
    ]
    result = _build_positions_panel(holdings)
    output = _render(result)
    assert "AAPL" in output


def test_build_decisions_sans_palette_ne_plante_pas() -> None:
    decisions = [
        {
            "symbol": "AAPL",
            "action": "BUY",
            "qty": 1.0,
            "rationale": "ok",
            "confidence": 0.8,
        }
    ]
    result = _build_decisions_table(decisions)
    output = _render(result)
    assert "AAPL" in output


def test_build_positions_panel_avec_light_ne_plante_pas() -> None:
    """Avec PALETTE_LIGHT, le builder ne plante pas et produit un output."""
    result = _build_positions_panel(
        [
            {
                "symbol": "AAPL",
                "quantity": 10.0,
                "avg_price": 170.0,
                "last_price": 180.0,
                "unrealized_pnl": 100.0,
            }
        ],
        palette=PALETTE_LIGHT,
    )
    output = _render(result)
    assert "AAPL" in output


def test_build_kpi_band_avec_light_ne_plante_pas() -> None:
    kpis = {
        "sharpe": 1.2,
        "max_drawdown": -0.05,
        "period_win_rate": 0.6,
        "volatility": 0.2,
        "num_trades": 10,
    }
    result = _build_kpi_band(kpis, palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None


def test_build_equity_panel_avec_light_ne_plante_pas() -> None:
    result = _build_equity_panel([100.0, 101.5, 99.0, 102.0], palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None


def test_build_attribution_avec_light_ne_plante_pas() -> None:
    result = _build_attribution_panel({}, palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None


def test_build_learnings_avec_light_ne_plante_pas() -> None:
    learnings = [{"note": "test", "symbol": "AAPL", "ts": "2026-06-10T10:00:00Z"}]
    result = _build_learnings_panel(learnings, palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None
