"""Tests TUI D10 — fonctions pures build_selection_panel, build_trades_table,
compute_realized_pnl_by_fill, _fmt_symbol_short, build_universe_panel.

TDD sur les fonctions de rendu et de calcul : données fixées, résultats
vérifiables sans sortie visuelle (pas de test Textual ici — rendu pur Rich).
"""

from __future__ import annotations

from rich.console import Console

from trader.ui.palette import PALETTE_DARK, PALETTE_LIGHT
from trader.tui import (
    build_selection_panel,
    build_trades_table,
    build_universe_panel,
    compute_realized_pnl_by_fill,
    _fmt_symbol_short,
)


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


class TestBuildClosedTradesTable:
    def test_affiche_trips_clotures_avec_nom_net_raison_et_duree(self):
        from trader.tui import build_closed_trades_table

        trips = [
            {
                "symbol": "AAPL",
                "side": "LONG",
                "entry_price": 180.0,
                "exit_price": 184.25,
                "gross_pnl": 43.5,
                "commission": 1.0,
                "pnl": 42.5,
                "commission_quality": {"status": "available"},
                "exit_reason": "take_profit:tp1",
                "holding_minutes": 135.0,
                "exit_ts": "2026-06-15T15:05:12+00:00",
            },
            {
                "symbol": "NVDA",
                "side": "SHORT",
                "entry_price": 900.0,
                "exit_price": 905.0,
                "gross_pnl": -24.0,
                "commission": 1.0,
                "pnl": -25.0,
                "commission_quality": {"status": "available"},
                "exit_reason": "trailing_stop_extraordinairement_long_a_tronquer",
                "holding_minutes": 9.0,
                "exit_ts": "2026-06-15T14:00:00+00:00",
            },
        ]

        result = build_closed_trades_table(
            trips,
            {"AAPL": "Apple Inc.", "NVDA": "NVIDIA Corporation"},
            limit=2,
            palette=PALETTE_DARK,
        )
        rendered = _render_to_str(result)

        assert "Sorties / Trades clôturés" in rendered
        assert "15:05:12" in rendered
        assert "Apple" in rendered and "AAPL" in rendered
        assert "LONG" in rendered
        assert "180.0000" in rendered and "184.2500" in rendered
        assert "+42.50" in rendered
        assert "take_profit:tp1" in rendered
        assert "2h15" in rendered
        assert "-25.00" in rendered
        assert "trailing_stop_extraordinairement_long_a_tronquer" not in rendered
        assert "..." in rendered
        net_col = next(
            col for col in result.columns if col.header == "Net USD / brut"
        )
        assert net_col._cells[0].style == PALETTE_DARK["pnl_positive"]
        assert net_col._cells[1].style == PALETTE_DARK["pnl_negative"]

    def test_tolere_liste_vide_et_champs_manquants(self):
        from trader.tui import build_closed_trades_table

        empty = build_closed_trades_table([], {}, palette=PALETTE_DARK)
        assert "—" in _render_to_str(empty)

        minimal = build_closed_trades_table(
            [{"symbol": "MSFT"}],
            {},
            palette=PALETTE_DARK,
        )
        rendered = _render_to_str(minimal)
        assert "MSFT" in rendered
        assert "—" in rendered


# ---------------------------------------------------------------------------
# Tests compute_realized_pnl_by_fill
# ---------------------------------------------------------------------------


class TestComputeRealizedPnlByFill:
    """Tests du rejeu FIFO coût moyen des fills."""

    def test_liste_vide(self):
        """Liste vide → liste vide."""
        result = compute_realized_pnl_by_fill([])
        assert result == []

    def test_buy_seul_retourne_none(self):
        """Un BUY seul → None (pas de réalisé)."""
        fills = [{"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "price": 100.0, "commission": 0.0}]
        result = compute_realized_pnl_by_fill(fills)
        assert result == [None]

    def test_achat_puis_vente_gagnante(self):
        """Achat à 100, vente à 110 → net = (110 - 100) * 10 = 100."""
        fills = [
            {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "price": 100.0, "commission": 0.0},
            {"symbol": "AAPL", "side": "SELL", "quantity": 10.0, "price": 110.0, "commission": 0.0},
        ]
        result = compute_realized_pnl_by_fill(fills)
        assert result[0] is None
        assert result[1] is not None
        assert abs(result[1] - 100.0) < 0.01

    def test_achat_puis_vente_perdante(self):
        """Achat à 100, vente à 90 → net = (90 - 100) * 10 = -100."""
        fills = [
            {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "price": 100.0, "commission": 0.0},
            {"symbol": "AAPL", "side": "SELL", "quantity": 10.0, "price": 90.0, "commission": 0.0},
        ]
        result = compute_realized_pnl_by_fill(fills)
        assert result[1] is not None
        assert abs(result[1] - (-100.0)) < 0.01

    def test_commission_reduit_le_gain(self):
        """Achat à 100 + commission 1, vente à 110 + commission 1 → net = 98."""
        fills = [
            {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "price": 100.0, "commission": 1.0},
            {"symbol": "AAPL", "side": "SELL", "quantity": 10.0, "price": 110.0, "commission": 1.0},
        ]
        result = compute_realized_pnl_by_fill(fills)
        # Coût moyen achat = (10 * 100 + 1) / 10 = 100.1
        # Net SELL = (110 - 100.1) * 10 - 1 = 99 - 1 = 98
        assert result[1] is not None
        assert abs(result[1] - 98.0) < 0.01

    def test_vente_partielle(self):
        """Achat 10 actions, vente partielle 5 → coût moyen inchangé pour le reste."""
        fills = [
            {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "price": 100.0, "commission": 0.0},
            {"symbol": "AAPL", "side": "SELL", "quantity": 5.0, "price": 110.0, "commission": 0.0},
            {"symbol": "AAPL", "side": "SELL", "quantity": 5.0, "price": 120.0, "commission": 0.0},
        ]
        result = compute_realized_pnl_by_fill(fills)
        assert result[0] is None
        # Première vente : (110 - 100) * 5 = 50
        assert abs(result[1] - 50.0) < 0.01
        # Deuxième vente : (120 - 100) * 5 = 100 (coût moyen inchangé = 100)
        assert abs(result[2] - 100.0) < 0.01

    def test_vente_sans_achat_retourne_none(self):
        """Vente sans achat préalable → None (pas de crash)."""
        fills = [{"symbol": "AAPL", "side": "SELL", "quantity": 5.0, "price": 110.0, "commission": 0.0}]
        result = compute_realized_pnl_by_fill(fills)
        assert result == [None]

    def test_longueur_egale_aux_fills(self):
        """La liste retournée a toujours la même longueur que fills."""
        fills = [
            {"symbol": "AAPL", "side": "BUY", "quantity": 5.0, "price": 100.0, "commission": 0.0},
            {"symbol": "NVDA", "side": "BUY", "quantity": 2.0, "price": 500.0, "commission": 0.0},
            {"symbol": "AAPL", "side": "SELL", "quantity": 5.0, "price": 110.0, "commission": 0.0},
        ]
        result = compute_realized_pnl_by_fill(fills)
        assert len(result) == len(fills)

    def test_multi_symboles_isoles(self):
        """Deux symboles distincts ne se mélangent pas."""
        fills = [
            {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "price": 100.0, "commission": 0.0},
            {"symbol": "NVDA", "side": "BUY", "quantity": 5.0, "price": 200.0, "commission": 0.0},
            {"symbol": "AAPL", "side": "SELL", "quantity": 10.0, "price": 110.0, "commission": 0.0},
            {"symbol": "NVDA", "side": "SELL", "quantity": 5.0, "price": 180.0, "commission": 0.0},
        ]
        result = compute_realized_pnl_by_fill(fills)
        assert result[0] is None  # BUY AAPL
        assert result[1] is None  # BUY NVDA
        assert abs(result[2] - 100.0) < 0.01   # SELL AAPL : (110-100)*10
        assert abs(result[3] - (-100.0)) < 0.01  # SELL NVDA : (180-200)*5

    def test_fill_sans_commission(self):
        """Un fill sans commission ne doit pas être certifié net à zéro frais."""
        fills = [
            {"symbol": "AAPL", "side": "BUY", "quantity": 10.0, "price": 100.0},
            {"symbol": "AAPL", "side": "SELL", "quantity": 10.0, "price": 110.0},
        ]
        result = compute_realized_pnl_by_fill(fills)
        assert result == [None, None]


# ---------------------------------------------------------------------------
# Tests _fmt_symbol_short
# ---------------------------------------------------------------------------


class TestFmtSymbolShort:
    """Tests du helper de formatage NOM · TICKER."""

    def test_ticker_absent_retourne_brut(self):
        """Ticker absent de company_map → ticker brut."""
        assert _fmt_symbol_short("AAPL", {}) == "AAPL"

    def test_nom_court_format_complet(self):
        """Nom court → contient le nom (troncature à 20 chars peut couper le ticker)."""
        result = _fmt_symbol_short("ASML.AS", {"ASML.AS": "ASML"})
        assert "ASML" in result

    def test_nom_long_premier_mot_seulement(self):
        """Nom long (> 15 chars) → premier mot seulement."""
        result = _fmt_symbol_short(
            "2330.TW",
            {"2330.TW": "Taiwan Semiconductor Manufacturing Company Limited"}
        )
        # Doit contenir "Taiwan" (premier mot) mais pas "Manufacturing"
        assert "Taiwan" in result
        assert "Manufacturing" not in result

    def test_tronque_a_20_chars(self):
        """Résultat tronqué à 20 chars max."""
        result = _fmt_symbol_short("AAPL", {"AAPL": "Apple Inc."})
        assert len(result) <= 20

    def test_map_vide(self):
        """company_map vide → ticker brut sans exception."""
        result = _fmt_symbol_short("NVDA", {})
        assert result == "NVDA"


# ---------------------------------------------------------------------------
# Tests build_universe_panel
# ---------------------------------------------------------------------------


_UNIVERSE_SYMBOLS = ["OR.PA", "ASML.AS", "CFR.SW", "AAPL", "NVDA"]

_VENUE_STATE_UNIVERSE = {
    "venues": {
        "EU": {
            "scores": {"OR.PA": 0.85, "ASML.AS": 0.92, "CFR.SW": 0.70},
        },
        "US": {
            "scores": {"AAPL": 0.60, "NVDA": 0.95},
        },
    }
}

_COMPANY_MAP = {
    "OR.PA": "L'Oréal S.A.",
    "ASML.AS": "ASML Holding N.V.",
    "CFR.SW": "Compagnie Financière Richemont SA",
    "AAPL": "Apple Inc.",
    "NVDA": "NVIDIA Corporation",
}


class TestBuildUniversePanel:
    def test_ne_leve_pas_avec_etat_minimal(self):
        """Ne doit pas lever avec un état minimal."""
        result = build_universe_panel(
            _UNIVERSE_SYMBOLS, _VENUE_STATE_UNIVERSE, ["EU"], _COMPANY_MAP,
            palette=PALETTE_DARK
        )
        assert result is not None

    def test_symboles_eu_presents(self):
        """Les symboles EU apparaissent dans le rendu."""
        result = build_universe_panel(
            _UNIVERSE_SYMBOLS, _VENUE_STATE_UNIVERSE, ["EU"], _COMPANY_MAP,
            palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        # Au moins un symbole EU doit être présent (avec son ticker brut ou le nom court)
        assert "OR.PA" in rendered or "L'Oréal" in rendered

    def test_symboles_us_presents(self):
        """Les symboles US apparaissent dans le rendu."""
        result = build_universe_panel(
            _UNIVERSE_SYMBOLS, _VENUE_STATE_UNIVERSE, [], _COMPANY_MAP,
            palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        assert "AAPL" in rendered or "Apple" in rendered

    def test_venue_ouverte_badge_ouvert(self):
        """Une venue ouverte doit afficher OUVERT dans son titre."""
        result = build_universe_panel(
            _UNIVERSE_SYMBOLS, _VENUE_STATE_UNIVERSE, ["EU", "US"], _COMPANY_MAP,
            palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        assert "OUVERT" in rendered

    def test_venue_fermee_badge_ferme(self):
        """Une venue fermée doit afficher fermé dans son titre."""
        result = build_universe_panel(
            _UNIVERSE_SYMBOLS, _VENUE_STATE_UNIVERSE, [], _COMPANY_MAP,
            palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        assert "fermé" in rendered

    def test_tolere_liste_vide(self):
        """Ne doit pas lever avec une liste de symboles vide."""
        result = build_universe_panel([], {}, [], {}, palette=PALETTE_DARK)
        assert result is not None

    def test_scores_presents(self):
        """Les scores doivent apparaître dans le rendu."""
        result = build_universe_panel(
            _UNIVERSE_SYMBOLS, _VENUE_STATE_UNIVERSE, [], _COMPANY_MAP,
            palette=PALETTE_DARK
        )
        rendered = _render_to_str(result)
        # Un score comme 0.9200 doit apparaître
        assert "0.92" in rendered or "0.85" in rendered

    def test_score_nan_affiche_tiret_plutot_que_nan(self):
        """Un score NaN est une donnée absente côté UI, jamais le texte 'nan'."""
        result = build_universe_panel(
            ["AAPL"],
            {"venues": {"US": {"scores": {"AAPL": float("nan")}}}},
            ["US"],
            {"AAPL": "Apple Inc."},
            palette=PALETTE_DARK,
        )
        rendered = _render_to_str(result)
        assert "—" in rendered
        assert "nan" not in rendered.lower()

    def test_palette_light_ne_leve_pas(self):
        """Doit fonctionner avec PALETTE_LIGHT sans exception."""
        result = build_universe_panel(
            _UNIVERSE_SYMBOLS, _VENUE_STATE_UNIVERSE, ["EU"], _COMPANY_MAP,
            palette=PALETTE_LIGHT
        )
        assert result is not None
