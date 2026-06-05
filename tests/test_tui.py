"""tests pour trader.tui — vérifie build_view sans I/O réseau ni boucle live."""

from __future__ import annotations

from rich.console import RenderableType

from trader.tui import build_view


# ---------------------------------------------------------------------------
# Fixtures de données
# ---------------------------------------------------------------------------

_FULL_STATE: dict = {
    "ts": "2026-06-05T12:00:00+00:00",
    "dry_run": True,
    "kill_switch": False,
    "portfolio": {
        "cash": 85_000.0,
        "equity": 102_500.0,
        "total_return_pct": 2.5,
        "holdings": [
            {
                "symbol": "AAPL",
                "quantity": 10.0,
                "avg_price": 170.0,
                "last_price": 185.0,
                "unrealized_pnl": 150.0,   # PnL positif → vert
            },
            {
                "symbol": "TSLA",
                "quantity": 5.0,
                "avg_price": 250.0,
                "last_price": 210.0,
                "unrealized_pnl": -200.0,  # PnL négatif → rouge
            },
        ],
    },
    "decisions": [
        {
            "symbol": "AAPL",
            "action": "BUY",
            "qty": 2.0,
            "rationale": "tendance haussière confirmée par momentum 20j",
            "confidence": 0.82,
            "executed": True,
            "reason": "ok",
        },
        {
            "symbol": "TSLA",
            "action": "HOLD",
            "qty": 0.0,
            "rationale": "attente signal de retournement",
            "confidence": 0.55,
            "executed": False,
            "reason": "hold",
        },
    ],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_view_avec_etat_complet_retourne_un_renderable() -> None:
    """build_view avec un dict complet retourne un RenderableType sans exception."""
    result = build_view(_FULL_STATE)
    # Le résultat doit être un objet affichable par Rich
    assert result is not None
    # Doit être rendu par la console sans planter
    from rich.console import Console
    console = Console(width=120)
    # Tenter d'écrire dans un buffer — si ça ne lève pas, c'est bon
    with console.capture() as capture:
        console.print(result)
    output = capture.get()
    # L'output doit contenir les données clé
    assert "AAPL" in output
    assert "TSLA" in output
    assert "dry" in output.lower() or "DRY" in output or "2.5" in output or "BUY" in output


def test_build_view_avec_none_retourne_un_renderable() -> None:
    """build_view avec None ne plante pas et retourne quelque chose d'affichable."""
    result = build_view(None)
    assert result is not None
    from rich.console import Console
    console = Console(width=120)
    with console.capture():
        console.print(result)  # ne doit pas lever


def test_build_view_avec_dict_partiel_sans_portfolio_retourne_un_renderable() -> None:
    """build_view avec un dict sans clé 'portfolio' ne plante pas."""
    partial_state: dict = {
        "ts": "2026-06-05T08:00:00+00:00",
        "dry_run": True,
        "decisions": [],
    }
    result = build_view(partial_state)
    assert result is not None
    from rich.console import Console
    console = Console(width=120)
    with console.capture() as capture:
        console.print(result)
    output = capture.get()
    # Pas de positions → doit afficher un placeholder
    assert "—" in output
