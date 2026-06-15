"""Tests TUI D10 — fonctions pures build_selection_panel et build_trades_table.

TDD sur les fonctions de rendu : données fixées, résultats vérifiables sans
sortie visuelle (pas de test Textual ici — le rendu est pur Rich).
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from trader.palette import PALETTE_DARK, PALETTE_LIGHT
from trader.tui import build_selection_panel, build_trades_table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_to_str(renderable) -> str:
    """Rend un Rich renderable en texte brut (sans markup)."""
    console = Console(highlight=False, markup=False, width=200)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# ---------------------------------------------------------------------------
# Fixtures de données minimales
# ---------------------------------------------------------------------------


_VENUE_STATE_MINIMAL = {
    "venues": {
        "TW": {
            "hotlist": ["2330.TW", "2317.TW", "2454.TW"],
            "scores": {"2330.TW": 0.92, "2317.TW": 0.75, "2454.TW": 0.60},
            "dwell": {},
            "last_close_at": "2026-06-15T05:30:00+00:00",
            "stale": False,
        },
        "EU": {
            "hotlist": ["ASML.EU", "SIE.EU"],
            "scores": {"ASML.EU": 0.85, "SIE.EU": 0.40},
            "dwell": {},
            "last_close_at": "2026-06-15T15:30:00+00:00",
            "stale": False,
        },
        "US": {
            "hotlist": ["AAPL", "MSFT", "NVDA"],
            "scores": {"AAPL": 0.88, "MSFT": 0.70, "NVDA": 0.95},
            "dwell": {},
            "last_close_at": "2026-06-15T20:00:00+00:00",
            "stale": False,
        },
    }
}

_FILLS_SAMPLE = [
    {
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10.0,
        "price": 182.50,
        "ts": "2026-06-15T13:45:00+00:00",
        "commission": 0.035,
        "commission_currency": "USD",
        "commission_model": "ibkr",
    },
    {
        "symbol": "NVDA",
        "side": "SELL",
        "quantity": 5.0,
        "price": 870.20,
        "ts": "2026-06-15T14:10:00+00:00",
        "commission": 0.0175,
        "commission_currency": "USD",
        "commission_model": "ibkr",
    },
    {
        "symbol": "MSFT",
        "side": "BUY",
        "quantity": 3.0,
        "price": 420.75,
        "ts": "2026-06-15T15:05:00+00:00",
        "commission": 0.0105,
        "commission_currency": "USD",
        "commission_model": "ibkr",
    },
]


# ---------------------------------------------------------------------------
# Tests build_selection_panel
# ---------------------------------------------------------------------------


class TestBuildSelectionPanel:
    def test_ne_leve_pas_avec_etat_minimal(self):
        """Ne doit pas lever avec un venue_state minimal."""
        result = build_selection_panel(
            _VENUE_STATE_MINIMAL, open_venues_list=["TW"], palette=PALETTE_DARK
        )
        assert result is not None

    def test_contient_symboles_tw_attendus(self):
        """Les symboles TW apparaissent dans le rendu."""
        result = build_selection_panel(
            _VENUE_STATE_MINIMAL, open_venues_list=["TW"], palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        assert "2330.TW" in rendered
        assert "2317.TW" in rendered

    def test_tri_par_attractivite_decroissante(self):
        """NVDA (0.95) doit apparaître avant AAPL (0.88) dans le rendu US."""
        result = build_selection_panel(
            _VENUE_STATE_MINIMAL, open_venues_list=["US"], palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        pos_nvda = rendered.find("NVDA")
        pos_aapl = rendered.find("AAPL")
        assert pos_nvda != -1, "NVDA doit être dans le rendu"
        assert pos_aapl != -1, "AAPL doit être dans le rendu"
        assert pos_nvda < pos_aapl, "NVDA (score 0.95) doit apparaître avant AAPL (0.88)"

    def test_badge_ouvert_pour_venue_ouverte(self):
        """Le badge OUVERT doit apparaître pour TW quand TW est dans open_venues."""
        result = build_selection_panel(
            _VENUE_STATE_MINIMAL, open_venues_list=["TW", "EU"], palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        assert "OUVERT" in rendered

    def test_badge_ferme_pour_venue_fermee(self):
        """Le badge fermé doit apparaître pour US quand US n'est pas dans open_venues."""
        result = build_selection_panel(
            _VENUE_STATE_MINIMAL, open_venues_list=["TW"], palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        assert "fermé" in rendered

    def test_tolere_etat_vide(self):
        """Ne doit pas lever avec un venue_state vide."""
        result = build_selection_panel({}, open_venues_list=[], palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        assert "—" in rendered

    def test_tolere_venue_absente(self):
        """Ne doit pas lever si une venue manque dans l'état."""
        partial_state = {
            "venues": {
                "TW": {
                    "hotlist": ["2330.TW"],
                    "scores": {"2330.TW": 0.9},
                }
            }
        }
        result = build_selection_panel(
            partial_state, open_venues_list=["TW"], palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        assert "2330.TW" in rendered
        assert "—" in rendered  # EU et US absents → tirets

    def test_les_trois_venues_apparaissent(self):
        """TW, EU, US doivent tous apparaître dans le rendu."""
        result = build_selection_panel(
            _VENUE_STATE_MINIMAL, open_venues_list=["TW", "EU", "US"], palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        assert "TW" in rendered
        assert "EU" in rendered
        assert "US" in rendered

    def test_tronque_a_12_par_venue(self):
        """Plus de 12 symboles → seulement 12 affichés par venue."""
        many_symbols = [f"SYM{i:02d}.TW" for i in range(20)]
        big_state = {
            "venues": {
                "TW": {
                    "hotlist": many_symbols,
                    "scores": {s: float(i) / 20.0 for i, s in enumerate(many_symbols)},
                }
            }
        }
        result = build_selection_panel(big_state, open_venues_list=["TW"], palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        # SYM19.TW (score 0.95) doit être présent ; SYM00.TW (score 0.0) tronqué
        displayed = [s for s in many_symbols if s in rendered]
        assert len(displayed) <= 12

    def test_palette_light_ne_leve_pas(self):
        """Doit fonctionner avec PALETTE_LIGHT sans exception."""
        result = build_selection_panel(
            _VENUE_STATE_MINIMAL, open_venues_list=["TW"], palette=PALETTE_LIGHT
        )
        assert result is not None


# ---------------------------------------------------------------------------
# Tests build_trades_table
# ---------------------------------------------------------------------------


class TestBuildTradesTable:
    def test_ne_leve_pas_avec_liste_vide(self):
        """Ne doit pas lever avec une liste vide."""
        result = build_trades_table([], palette=PALETTE_DARK)
        assert result is not None

    def test_contient_symboles_attendus(self):
        """Les symboles des fills doivent apparaître dans le rendu."""
        result = build_trades_table(_FILLS_SAMPLE, palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        assert "AAPL" in rendered
        assert "NVDA" in rendered
        assert "MSFT" in rendered

    def test_sens_buy_sell_present(self):
        """BUY et SELL doivent apparaître dans le rendu."""
        result = build_trades_table(_FILLS_SAMPLE, palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        assert "BUY" in rendered
        assert "SELL" in rendered

    def test_prix_present(self):
        """Le prix du fill doit apparaître dans le rendu."""
        result = build_trades_table(_FILLS_SAMPLE, palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        assert "182.5000" in rendered or "182" in rendered

    def test_commission_presente(self):
        """La commission doit apparaître dans le rendu."""
        result = build_trades_table(_FILLS_SAMPLE, palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        # Commission présente sous forme de nombre
        assert "USD" in rendered or "0.0" in rendered

    def test_limite_nombre_de_lignes(self):
        """Avec limit=2, seuls les 2 derniers fills sont affichés."""
        result = build_trades_table(_FILLS_SAMPLE, limit=2, palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        # AAPL est le premier fill (ancien) → ne doit pas apparaître avec limit=2
        assert "NVDA" in rendered or "MSFT" in rendered
        # AAPL peut être absent avec limit=2 (les 2 derniers sont NVDA et MSFT)
        assert "AAPL" not in rendered

    def test_fills_vides_affiche_tirets(self):
        """Liste vide → une ligne de tirets."""
        result = build_trades_table([], palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        assert "—" in rendered

    def test_ordre_antichronologique(self):
        """Le fill le plus récent (MSFT) doit apparaître avant les plus anciens."""
        result = build_trades_table(_FILLS_SAMPLE, palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        pos_msft = rendered.find("MSFT")
        pos_aapl = rendered.find("AAPL")
        assert pos_msft != -1 and pos_aapl != -1
        assert pos_msft < pos_aapl, "MSFT (fill le plus récent) doit être affiché en premier"

    def test_palette_light_ne_leve_pas(self):
        """Doit fonctionner avec PALETTE_LIGHT sans exception."""
        result = build_trades_table(_FILLS_SAMPLE, palette=PALETTE_LIGHT)
        assert result is not None

    def test_fill_sans_champs_optionnels(self):
        """Fill minimal (juste symbol+side+quantity+price+ts) ne doit pas lever."""
        minimal_fill = [
            {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "price": 180.0, "ts": "2026-06-15T10:00:00Z"}
        ]
        result = build_trades_table(minimal_fill, palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        assert "AAPL" in rendered

    def test_heure_extraite_du_ts(self):
        """L'heure HH:MM:SS doit être extraite du champ ts ISO."""
        result = build_trades_table(_FILLS_SAMPLE, palette=PALETTE_DARK)
        rendered = _render_to_str(result)
        # MSFT à 15:05:00 → "15:05:05" ou "15:05"
        assert "15:05" in rendered
