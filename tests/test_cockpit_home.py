"""Tests des builders purs de la nouvelle home (home.py)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trader.ui.palette import PALETTE_LIGHT

UTC = timezone.utc
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


@dataclass
class FakeVital:
    status: str = "alive"
    battement_old: bool = False
    since_seconds: float | None = 2.0


STATE = {
    "portfolio": {"cash": 62589.0, "equity": 99452.0, "holdings": []},
    "starting_cash": 100000.0,
    "dry_run": True,
    "daemon_status": {"model_calls_used": 2, "max_model_calls_per_cycle": 25,
                      "decisions_done": 3, "symbols_total": 10},
    "equity_curve": [100000.0, 99500.0, 99452.0],
    "sessions": {"EU": {"open": "07:00", "close": "15:30"}},
}


def test_status_line_complet_a_largeur_confortable():
    from trader.cockpit.home import build_status_line

    line = build_status_line(STATE, kill_active=False, palette=PALETTE_LIGHT,
                             width=300, now=NOW, vital=FakeVital())
    plain = line.plain
    for fragment in ("VIVANT", "99,452", "DRY", "kill", "LLM 2/25", "cycle 3/10", "EU"):
        assert fragment in plain, fragment


def test_status_line_etroit_garde_les_criticites():
    """À 80 colonnes : vital/équité/mode/kill présents, sparkline droppée."""
    from trader.cockpit.home import build_status_line

    line = build_status_line(STATE, kill_active=True, palette=PALETTE_LIGHT,
                             width=80, now=NOW, vital=FakeVital())
    plain = line.plain
    assert "VIVANT" in plain and "99,452" in plain
    assert "DRY" in plain and "KILL" in plain
    assert "▁" not in plain and "█" not in plain  # sparkline droppée
    assert line.cell_len <= 80


def test_status_line_cycle_absent_hors_batch():
    from trader.cockpit.home import build_status_line

    state = {**STATE, "daemon_status": {"model_calls_used": 0,
                                        "max_model_calls_per_cycle": 25,
                                        "decisions_done": 0, "symbols_total": 0}}
    line = build_status_line(state, kill_active=False, palette=PALETTE_LIGHT,
                             width=300, now=NOW, vital=FakeVital())
    assert "cycle 0/0" not in line.plain


def test_attention_line_ras_et_anomalies():
    from trader.cockpit.home import build_attention_text

    assert "RAS" in build_attention_text({}, kill_active=False, palette=PALETTE_LIGHT, now=NOW).plain

    state = {"stale_streaks": {"AAA": 2}}
    rendered = build_attention_text(state, kill_active=True, palette=PALETTE_LIGHT, now=NOW).plain
    assert "KILL" in rendered and "stale" in rendered


def _patch_state_paths(monkeypatch, tmp_path):
    """Monkeypatch des constantes de chemins (même mécanique que test_cockpit_smoke)."""
    import json

    import trader.cockpit.app as cockpit_module
    import trader.read_models.runtime_state as rs

    (tmp_path / "current_report.json").write_text(json.dumps({
        "ts": "2026-07-03T10:00:00+00:00",
        "dry_run": True,
        "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
        "decisions": [],
    }), encoding="utf-8")
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")
    monkeypatch.setattr(rs, "_STATE_DIR", tmp_path)


def _console_render(renderable) -> str:
    from rich.console import Console

    console = Console(width=100, record=True)
    console.print(renderable)
    return console.export_text()


def test_build_activity_tile_series_et_compteurs():
    from trader.cockpit.home import build_activity_tile

    buckets = {"exec": [0] * 11 + [2], "veille": [0] * 12, "plan": [0] * 12,
               "risk": [1] + [0] * 11, "stale": [0] * 12, "hold": [3] * 12}
    rendered = _console_render(build_activity_tile(buckets, palette=PALETTE_LIGHT))
    assert "exec" in rendered and "n=2" in rendered
    assert "risk" in rendered and "n=1" in rendered
    assert "n=36" in rendered  # hold


def test_build_activity_tile_serie_vide_en_pointilles():
    """Série à zéro → pointillés dim, pas une fausse barre pleine."""
    from trader.cockpit.home import build_activity_tile

    buckets = {state: [0] * 12 for state in
               ("exec", "veille", "plan", "risk", "stale", "hold")}
    rendered = _console_render(build_activity_tile(buckets, palette=PALETTE_LIGHT))
    assert "············" in rendered
    assert "▄" not in rendered


def test_status_line_sans_equite_pas_de_pnl_fantome():
    from trader.cockpit.home import build_status_line

    state = {**STATE, "portfolio": {"cash": 100000.0, "equity": 0.0, "holdings": []}}
    line = build_status_line(state, kill_active=False, palette=PALETTE_LIGHT,
                             width=300, now=NOW, vital=FakeVital())
    assert "-100,000" not in line.plain


def test_build_portfolio_summary_sans_positions():
    """build_portfolio_summary ne contient PAS la table positions (déplacée vers PositionsTable)."""
    from trader.cockpit.home import build_portfolio_summary

    state = {
        "portfolio": {"cash": 50000.0, "equity": 100000.0, "holdings": [
            {"symbol": "AAA", "quantity": 10, "avg_price": 90.0,
             "last_price": 100.0, "fx_rate": 1.0, "unrealized_pnl": 100.0},
        ]},
        "trade_plans": [{"symbol": "AAA", "side": "LONG", "hard_stop_price": 95.0,
                         "remaining_quantity": 10}],
        "equity_curve": [100000.0, 100100.0],
        "attribution": {},
        "kpis": {},
        "starting_cash": 100000.0,
    }
    rendered = _console_render(build_portfolio_summary(state, palette=PALETTE_LIGHT))
    assert "AAA" in rendered            # apparaît dans allocation/contrib
    assert "Perte@stop" not in rendered  # colonne déplacée vers PositionsTable
    assert "Risque sorties" not in rendered  # plus de table séparée clippée


def test_build_plans_tile_ordre_types():
    from trader.cockpit.home import build_plans_tile

    state = {
        "armed_plans": [{"symbol": "ARM.TW", "order": {"intent": "OPEN_LONG", "qty": 4},
                         "conditions": [], "expires_at": (NOW + timedelta(hours=2)).isoformat()}],
        "trade_plans": [{"symbol": "EXI.TW", "side": "LONG", "hard_stop_price": 1.0,
                         "entry_price": 2.0, "remaining_quantity": 1}],
        "indicator_watches": [{"symbol": "WCH.TW", "conditions": [],
                               "expires_at": (NOW + timedelta(hours=3)).isoformat()}],
    }
    rendered = _console_render(build_plans_tile(state, palette=PALETTE_LIGHT, now=NOW))
    assert rendered.index("ARM.TW") < rendered.index("EXI.TW") < rendered.index("WCH.TW")
    assert "armé" in rendered and "sortie" in rendered and "veille" in rendered


def test_hex_to_rgb():
    from trader.cockpit.home import _hex_to_rgb

    assert _hex_to_rgb("#0D7680") == (13, 118, 128)
    assert _hex_to_rgb("cyan") == "cyan"


def test_build_plans_tile_arme_pas_duplique_apres_round_trip_json():
    """Un plan armé (présent dans armed_plans ET indicator_watches) n'apparaît
    qu'une fois, même après round-trip JSON (objets différents).

    Note fixture : is_armed_plan vérifie on_trigger == "EXECUTE_ORDER" et
    isinstance(order, dict) — utiliser on_trigger (pas trigger_action).
    """
    import json as _json
    from trader.cockpit.home import build_plans_tile

    state = {
        "armed_plans": [{"symbol": "ARM.TW", "on_trigger": "EXECUTE_ORDER",
                         "order": {"intent": "OPEN_LONG", "qty": 4}, "conditions": [],
                         "expires_at": (NOW + timedelta(hours=2)).isoformat()}],
        "indicator_watches": [{"symbol": "ARM.TW", "on_trigger": "EXECUTE_ORDER",
                               "order": {"intent": "OPEN_LONG", "qty": 4}, "conditions": [],
                               "expires_at": (NOW + timedelta(hours=2)).isoformat()},
                              {"symbol": "WCH.TW", "conditions": [],
                               "expires_at": (NOW + timedelta(hours=3)).isoformat()}],
        "trade_plans": [],
    }
    state = _json.loads(_json.dumps(state))
    rendered = _console_render(build_plans_tile(state, palette=PALETTE_LIGHT, now=NOW))
    assert rendered.count("ARM.TW") == 1   # une seule ligne armé, pas de doublon veille
    assert "WCH.TW" in rendered            # la veille simple reste listée


async def test_app_compose_attention_line(tmp_path, monkeypatch):
    from trader.cockpit.home import AttentionLine
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        assert app.query_one("#attention-line", AttentionLine) is not None


async def test_home_pane_remplace_overview(tmp_path, monkeypatch):
    from trader.cockpit.app import FluxPane
    from trader.cockpit.home import HomePane
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        home = app.query_one("#overview-page", HomePane)
        assert home is not None
        assert app.query_one("#home-flux", FluxPane) is not None
        # les tuiles existent
        for tile_id in ("#home-portfolio", "#home-activity", "#home-decisions", "#home-plans"):
            assert home.query_one(tile_id) is not None


def test_home_tuiles_sans_champs_runtime():
    """Anti-redondance (spec §8.6) : phase/LLM/cycle/source vivent dans le statut,
    plus jamais dans les tuiles."""
    from datetime import timedelta

    from trader.cockpit.home import build_activity_tile, build_plans_tile, build_portfolio_summary
    from trader.cockpit.aggregates import activity_buckets

    state = {
        "portfolio": {"cash": 1.0, "equity": 1.0, "holdings": []},
        "daemon_status": {"phase": "analyzing_batch", "model_calls_used": 9,
                          "max_model_calls_per_cycle": 25},
        "source": "current_report",
        "ts": "2026-07-03T10:00:00Z",
        "armed_plans": [], "trade_plans": [], "indicator_watches": [],
        "attribution": {}, "kpis": {}, "equity_curve": [],
    }
    rendered = "".join(
        _console_render(build)
        for build in (
            build_portfolio_summary(state, palette=PALETTE_LIGHT),
            build_activity_tile(activity_buckets([], NOW), palette=PALETTE_LIGHT),
            build_plans_tile(state, palette=PALETTE_LIGHT, now=NOW),
        )
    )
    assert "analyzing_batch" not in rendered
    assert "current_report" not in rendered
    assert "9/25" not in rendered


async def test_home_flux_recoit_les_events(tmp_path, monkeypatch):
    import json

    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"ts": "2026-07-03T01:00:00Z", "event": "decision_recorded",
                                  "symbol": "AAA", "action": "BUY", "executed": True}) + "\n",
                      encoding="utf-8")
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        from textual.widgets import RichLog

        flux_log = app.query_one("#home-flux").query_one(RichLog)
        assert len(flux_log.lines) > 0  # RichLog stocke les lignes dans .lines (pas .line_count)


def test_palette_ink_complete():
    from trader.ui.palette import ALL_PALETTE_KEYS, PALETTE_INK

    assert set(PALETTE_INK.keys()) == set(ALL_PALETTE_KEYS)


async def test_theme_defaut_est_ink(tmp_path, monkeypatch):
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        assert app.theme == "casys-ink"


async def test_binding_d_cycle_trois_themes(tmp_path, monkeypatch):
    """La touche d parcourt le cycle ink → glass → salmon → ink (3 états)."""
    import json
    import trader.cockpit.supervisor as sup_module

    _patch_state_paths(monkeypatch, tmp_path)
    # Daemon vivant → évite le ConfirmStart modal qui intercepterait les touches
    (tmp_path / "daemon_status.json").write_text(
        json.dumps({"ts": "2026-07-03T10:00:00Z", "phase": "idle", "pid": 54321}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sup_module, "_is_daemon_pid", lambda p: p == 54321)

    from trader.cockpit import CockpitApp

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        assert app.theme == "casys-ink"
        await pilot.press("d")
        assert app.theme == "casys-glass"
        assert app.screen.has_class("glass")
        await pilot.press("d")
        assert app.theme == "casys-salmon"
        assert not app.screen.has_class("glass")
        await pilot.press("d")
        assert app.theme == "casys-ink"


def test_build_portfolio_summary_sans_equite_pas_de_pnl_fantome():
    from trader.cockpit.home import build_portfolio_summary

    state = {"portfolio": {"cash": 100000.0, "equity": 0.0, "holdings": []},
             "starting_cash": 100000.0, "kpis": {}, "attribution": {}, "equity_curve": []}
    rendered = _console_render(build_portfolio_summary(state, palette=PALETTE_LIGHT))
    assert "-100,000" not in rendered


def test_build_symbol_detail_sections():
    from trader.cockpit.home import build_symbol_detail
    from trader.ui.palette import PALETTE_LIGHT

    state = {
        "portfolio": {"holdings": [{"symbol": "AAA.TW", "quantity": 10,
                                    "avg_price": 90.0, "last_price": 100.0}]},
        "trade_plans": [{"symbol": "AAA.TW", "side": "LONG", "hard_stop_price": 95.0,
                         "entry_price": 90.0, "remaining_quantity": 10}],
        "recent_decisions": [{"symbol": "AAA.TW", "action": "BUY", "executed": True,
                              "ts": "2026-07-03T09:00:00Z"},
                             {"symbol": "BBB.TW", "action": "HOLD",
                              "ts": "2026-07-03T09:00:00Z"}],
        "learnings": [{"symbol": "AAA.TW", "note": "gap à l'open fréquent",
                       "ts": "2026-07-01T00:00:00Z"}],
        "stale_streaks": {"AAA.TW": 2},
    }
    rendered = _console_render(build_symbol_detail(state, "AAA.TW", palette=PALETTE_LIGHT))
    assert "AAA.TW" in rendered
    assert "95" in rendered                    # plan de sortie
    assert "gap à l'open" in rendered          # learning du symbole
    assert "BBB.TW" not in rendered            # filtré par symbole
    assert "stale" in rendered.lower()         # santé data
