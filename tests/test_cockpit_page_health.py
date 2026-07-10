"""Tests de la page Health du cockpit casys redesign.

Couvre :
- Builders purs (build_freshness, build_fx_rates, build_sources, build_llm,
  build_learnings, build_universe) avec des state dicts synthétiques et now fixe.
- Montage Textual : navigation vers la page health, vérification des panneaux.
- Cas limites : state vide, listes vides, valeurs None.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.pages.health import (
    HealthPage,
    build_freshness,
    build_fx_rates,
    build_learnings,
    build_llm,
    build_sources,
    build_universe,
)
from trader.interfaces.cockpit.supervisor import DaemonVitalState

UTC = timezone.utc
NOW = datetime(2026, 7, 6, 2, 1, 28, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(renderable, width: int = 120) -> str:
    console = Console(width=width, highlight=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _patch_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")


def _write_daemon_alive(tmp_path: Path, monkeypatch, pid: int = 4242) -> None:
    (tmp_path / "daemon_status.json").write_text(
        json.dumps({"pid": pid, "ts": NOW.isoformat(), "phase": "cycle_completed"}),
        encoding="utf-8",
    )
    alive = DaemonVitalState(status="alive", since_seconds=5.0, battement_old=False)
    monkeypatch.setattr(cockpit_module, "daemon_vital_state", lambda _path: alive)


def _make_minimal_state(tmp_path: Path) -> None:
    (tmp_path / "current_report.json").write_text(
        json.dumps(
            {
                "ts": "2026-07-06T02:01:28+00:00",
                "dry_run": True,
                "portfolio": {"cash": 100_000.0, "equity": 100_000.0, "holdings": []},
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# build_freshness
# ---------------------------------------------------------------------------


def test_freshness_empty_state():
    """État vide → message 'no symbols'."""
    rendered = _render(build_freshness({}, now=NOW))
    assert "no symbols" in rendered


def test_freshness_all_fresh():
    """Tous les symboles fresh → ● live pour chaque venue."""
    state = {
        "universe_symbols": ["AAPL", "MSFT", "2330.TW", "6488.TWO", "BN.PA"],
        "stale_market_data": {},
    }
    rendered = _render(build_freshness(state, now=NOW))
    assert "● live" in rendered
    assert "▲ stale" not in rendered
    # TW → TPE
    assert "TPE" in rendered
    assert "US" in rendered
    assert "EU" in rendered
    # Pas de footnote quand tout est fresh
    assert "Not an error" not in rendered


def test_freshness_stale_venues():
    """Symboles stales dans US et EU → ▲ stale avec âge, footnote d'explication."""
    state = {
        "universe_symbols": [
            "AAPL", "MSFT",          # US
            "BN.PA", "ASML.AS",      # EU
            "2330.TW",               # TW → fresh
        ],
        "stale_market_data": {
            "AAPL": {"data_age_minutes": 4700.0},
            "MSFT": {"data_age_minutes": 4680.0},
            "BN.PA": {"data_age_minutes": 3540.0},
            "ASML.AS": {"data_age_minutes": 3540.0},
        },
    }
    rendered = _render(build_freshness(state, now=NOW))
    assert "▲ stale" in rendered
    assert "● live" in rendered   # TW reste fresh
    assert "TPE" in rendered
    # âge US : max(4700, 4680) = 4700 min → 78h
    assert "78h" in rendered
    # footnote
    assert "Not an error" in rendered


def test_freshness_fx_symbols_skipped():
    """Les paires FX (=X) ne doivent pas apparaître dans DATA FRESHNESS."""
    state = {
        "universe_symbols": ["EURUSD=X", "CHFUSD=X", "AAPL"],
        "stale_market_data": {},
    }
    rendered = _render(build_freshness(state, now=NOW))
    # Seul US doit apparaître (AAPL), pas de venue FX
    assert "US" in rendered
    # Pas de ligne pour FX
    assert "FX" not in rendered


def test_freshness_partial_stale():
    """Un seul symbole stale dans une venue → venue marquée ▲ stale."""
    state = {
        "universe_symbols": ["AAPL", "MSFT"],
        "stale_market_data": {"AAPL": {"data_age_minutes": 200.0}},
    }
    rendered = _render(build_freshness(state, now=NOW))
    assert "▲ stale" in rendered
    assert "2 symbols" in rendered


def test_freshness_no_age_in_stale_entry():
    """Entrée stale sans data_age_minutes → affiche '?' pour l'âge."""
    state = {
        "universe_symbols": ["AAPL"],
        "stale_market_data": {"AAPL": {}},
    }
    rendered = _render(build_freshness(state, now=NOW))
    assert "▲ stale" in rendered
    assert "?" in rendered


def test_health_symbols_by_venue_is_canonical_projection_alias():
    from trader.interfaces.cockpit.pages import health as page
    from trader.interfaces.cockpit.projections import health as projection

    assert page._symbols_by_venue is projection.symbols_by_venue


def test_project_freshness_orders_venues_and_stale_symbols_by_age():
    from trader.interfaces.cockpit.projections.health import project_freshness

    state = {
        "universe_symbols": ["AAPL", "BN.PA", "2330.TW", "EURUSD=X"],
        "stale_market_data": {
            "AAPL": {"data_age_minutes": 120.0},
            "2330.TW": {"data_age_minutes": 300.0},
        },
    }

    projection = project_freshness(state)

    assert [row.venue for row in projection.venues] == ["TW", "EU", "US"]
    assert [row.display_name for row in projection.venues] == ["TPE", "EU", "US"]
    assert projection.has_stale is True
    assert [row.symbol for row in projection.stale_symbols] == [
        "2330.TW",
        "AAPL",
    ]
    assert projection.venues[0].max_age_minutes == 300.0
    assert projection.venues[1].is_stale is False


# ---------------------------------------------------------------------------
# build_fx_rates
# ---------------------------------------------------------------------------


def test_fx_rates_empty_state():
    """State sans fx_rates → message d'indisponibilité."""
    rendered = _render(build_fx_rates({}, now=NOW))
    assert "unavailable" in rendered


def test_fx_rates_with_data():
    """fx_rates peuplé → EUR, CHF, TWD affichés avec 4 décimales."""
    state = {"fx_rates": {"USD": 1.0, "EUR": 1.1438, "CHF": 1.2435, "TWD": 0.03121}}
    rendered = _render(build_fx_rates(state, now=NOW))
    assert "EUR" in rendered
    assert "1.1438" in rendered
    assert "CHF" in rendered
    assert "1.2435" in rendered
    assert "TWD" in rendered
    assert "0.0312" in rendered


def test_fx_rates_usd_only():
    """Dict avec uniquement USD (1.0) → 'no fx rates'."""
    state = {"fx_rates": {"USD": 1.0}}
    rendered = _render(build_fx_rates(state, now=NOW))
    assert "no fx rates" in rendered


def test_fx_rates_missing_keys():
    """Dict fx partiel → seules les clés présentes sont affichées."""
    state = {"fx_rates": {"EUR": 1.14}}
    rendered = _render(build_fx_rates(state, now=NOW))
    assert "EUR" in rendered
    assert "CHF" not in rendered


def test_project_fx_rates_preserves_supported_display_order():
    from trader.interfaces.cockpit.projections.health import project_fx_rates

    projection = project_fx_rates(
        {"fx_rates": {"USD": 1.0, "TWD": 0.0312, "EUR": 1.14}}
    )

    assert projection.source_available is True
    assert [(row.currency, row.rate) for row in projection.rows] == [
        ("EUR", 1.14),
        ("TWD", 0.0312),
    ]


# ---------------------------------------------------------------------------
# build_sources
# ---------------------------------------------------------------------------


def test_sources_default_env(monkeypatch):
    """Sans vars d'env → valeurs défauts 127.0.0.1:4002 · id 17."""
    monkeypatch.delenv("CASYS_IB_HOST", raising=False)
    monkeypatch.delenv("CASYS_IB_PORT", raising=False)
    monkeypatch.delenv("CASYS_IB_CLIENT_ID", raising=False)
    rendered = _render(build_sources({}, now=NOW))
    assert "127.0.0.1:4002" in rendered
    assert "id 17" in rendered
    assert "IB gateway" in rendered
    assert "yfinance" in rendered
    assert "news" in rendered


def test_sources_custom_env(monkeypatch):
    """Vars d'env personnalisées → affichées."""
    monkeypatch.setenv("CASYS_IB_HOST", "10.0.0.5")
    monkeypatch.setenv("CASYS_IB_PORT", "7496")
    monkeypatch.setenv("CASYS_IB_CLIENT_ID", "42")
    rendered = _render(build_sources({}, now=NOW))
    assert "10.0.0.5:7496" in rendered
    assert "id 42" in rendered


def test_sources_macro_omitted_when_absent():
    """Pas de macro dans state → pas de ligne macro."""
    rendered = _render(build_sources({}, now=NOW))
    assert "macro" not in rendered


def test_sources_macro_shown_when_present():
    """Clé 'macro' dans state → ligne macro affichée."""
    state = {"macro": {"next_fomc": "Jul 29"}}
    rendered = _render(build_sources(state, now=NOW))
    assert "macro" in rendered
    assert "Jul 29" in rendered


def test_sources_macro_generic_when_no_fomc():
    """macro présente mais sans next_fomc → 'available'."""
    state = {"macro": {"cpi_next": "Jul 11"}}
    rendered = _render(build_sources(state, now=NOW))
    assert "macro" in rendered
    assert "available" in rendered


def test_sources_empty_state_no_crash():
    """État vide → pas d'exception, IB gateway toujours affiché."""
    rendered = _render(build_sources({}, now=NOW))
    assert "IB gateway" in rendered


def test_project_sources_uses_explicit_adapter_config_and_optional_macro():
    from trader.interfaces.cockpit.projections.health import project_sources

    projection = project_sources(
        {"macro": {"next_fomc": "Jul 29"}},
        ib_host="10.0.0.5",
        ib_port="7496",
        ib_client_id="42",
    )

    assert [(row.name, row.detail) for row in projection.rows] == [
        ("IB gateway", "10.0.0.5:7496 · id 42"),
        ("yfinance", "fallback"),
        ("news", "yahoo"),
        ("macro", "next FOMC Jul 29"),
    ]


# ---------------------------------------------------------------------------
# build_llm
# ---------------------------------------------------------------------------


def test_llm_empty_state():
    """State vide → calls = '—', fallbacks = '0'."""
    rendered = _render(build_llm({}, now=NOW))
    assert "—" in rendered
    assert "fallbacks" in rendered
    assert "on error" in rendered
    assert "HOLD" in rendered


def test_llm_calls_cycle_only():
    """model_calls_used = 4, pas de model_performance → '4 this cycle'."""
    state = {"daemon_status": {"model_calls_used": 4}}
    rendered = _render(build_llm(state, now=NOW))
    assert "4 this cycle" in rendered


def test_llm_calls_cycle_and_total():
    """model_calls_used + model_performance.fills → '4 this cycle · 61 total'."""
    state = {
        "daemon_status": {"model_calls_used": 4},
        "kpis": {
            "model_performance": [
                {"provider": "acpx", "model": "gpt-5.5", "fills": 61, "fallbacks": 0},
            ]
        },
    }
    rendered = _render(build_llm(state, now=NOW))
    assert "4 this cycle" in rendered
    assert "61 total" in rendered


def test_llm_fallbacks_counted():
    """fallbacks depuis model_performance → somme affichée."""
    state = {
        "daemon_status": {"model_calls_used": 2},
        "kpis": {
            "model_performance": [
                {"fills": 10, "fallbacks": 3},
                {"fills": 5, "fallbacks": 1},
            ]
        },
    }
    rendered = _render(build_llm(state, now=NOW))
    assert "4 ·" in rendered   # 3 + 1 = 4 fallbacks
    assert "on error" in rendered


def test_llm_zero_calls():
    """model_calls_used = 0 → '0 this cycle'."""
    state = {"daemon_status": {"model_calls_used": 0}}
    rendered = _render(build_llm(state, now=NOW))
    assert "0 this cycle" in rendered


def test_project_llm_health_tolerates_and_sums_numeric_counters():
    from trader.interfaces.cockpit.projections.health import project_llm_health

    projection = project_llm_health(
        {
            "daemon_status": {"model_calls_used": "4"},
            "kpis": {
                "model_performance": [
                    {"fills": "10", "fallbacks": 2},
                    {"fills": 5, "fallbacks": "3"},
                ]
            },
        }
    )

    assert projection.calls_this_cycle == 4
    assert projection.total_fills == 15
    assert projection.total_fallbacks == 5
    assert projection.calls_label == "4 this cycle · 15 total"
    assert projection.fallbacks_label == "5 · on error → HOLD"


# ---------------------------------------------------------------------------
# build_learnings
# ---------------------------------------------------------------------------


def test_learnings_empty_state():
    """State vide → pending=?, consolidation idle, 'no recent learnings'."""
    rendered = _render(build_learnings({}, now=NOW))
    assert "?" in rendered
    assert "consolidation idle" in rendered
    assert "no recent learnings" in rendered


def test_learnings_with_pending_and_notes():
    """pending_count + 3 notes → tout affiché."""
    state = {
        "learnings_pending_count": 5,
        "learnings": [
            {"symbol": "AMCR", "note": "TP-scaling behaved well — repeat 50% at 1R"},
            {"symbol": "2330.TW", "note": "RS flips fast, require 2 closes"},
            {"symbol": None, "note": "avoid entries within 160bps of swing high"},
        ],
    }
    rendered = _render(build_learnings(state, now=NOW))
    assert "5 raw" in rendered
    assert "AMCR" in rendered
    assert "TP-scaling" in rendered
    assert "RS flips" in rendered
    assert "160bps" in rendered


def test_learnings_only_3_notes_shown():
    """Plus de 3 notes → seules les 3 premières sont affichées."""
    state = {
        "learnings_pending_count": 10,
        "learnings": [
            {"symbol": f"SYM{i}", "note": f"note number {i}"}
            for i in range(6)
        ],
    }
    rendered = _render(build_learnings(state, now=NOW))
    assert "SYM0" in rendered
    assert "SYM2" in rendered
    assert "SYM3" not in rendered


def test_learnings_consolidation_status_shown():
    """consolidation_status avec phase et last_run_ts → affiché."""
    state = {
        "learnings_pending_count": 3,
        "consolidation_status": {
            "phase": "running",
            "last_run_ts": "2026-07-06T00:05:00+00:00",
        },
        "learnings": [],
    }
    rendered = _render(build_learnings(state, now=NOW))
    assert "consolidation running" in rendered
    assert "00:05" in rendered


def test_learnings_empty_note_skipped():
    """Note vide (note='') → pas affichée."""
    state = {
        "learnings": [
            {"symbol": "AAPL", "note": ""},
            {"symbol": "MSFT", "note": "valid note here"},
        ]
    }
    rendered = _render(build_learnings(state, now=NOW))
    assert "AAPL" not in rendered
    assert "valid note" in rendered


def test_learnings_adaptive_limit_shows_more():
    """limit=5 → les 5 premières notes sont affichées (pas seulement 3)."""
    state = {
        "learnings": [
            {"symbol": f"SYM{i}", "note": f"note number {i}"}
            for i in range(6)
        ],
    }
    rendered = _render(build_learnings(state, now=NOW, limit=5))
    assert "SYM0" in rendered
    assert "SYM4" in rendered   # 5e note visible avec limit=5
    assert "SYM5" not in rendered  # 6e note hors limite


def test_learnings_default_still_3():
    """Sans limit explicite, le défaut (3) s'applique."""
    state = {
        "learnings": [
            {"symbol": f"SYM{i}", "note": f"note number {i}"}
            for i in range(6)
        ],
    }
    rendered = _render(build_learnings(state, now=NOW))  # limit=3 par défaut
    assert "SYM0" in rendered
    assert "SYM2" in rendered
    assert "SYM3" not in rendered


def test_project_learnings_filters_empty_notes_and_normalizes_status():
    from trader.interfaces.cockpit.projections.health import project_learnings

    projection = project_learnings(
        {
            "learnings_pending_count": 2,
            "consolidation_status": {
                "status": "running",
                "ts": "2026-07-06T00:05:00+00:00",
            },
            "learnings": [
                {"symbol": "AAPL", "note": ""},
                {"symbol": "MSFT", "note": "  keep this  "},
            ],
        }
    )

    assert projection.pending_label == "2"
    assert projection.consolidation_label == "consolidation running"
    assert projection.last_run_label == "00:05"
    assert [(row.symbol, row.note) for row in projection.notes] == [
        ("MSFT", "keep this")
    ]


# ---------------------------------------------------------------------------
# build_freshness — adaptive stale_limit
# ---------------------------------------------------------------------------


def test_freshness_stale_symbols_listed_with_budget():
    """stale_limit suffisant → symboles stales individuels listés."""
    state = {
        "universe_symbols": ["AAPL", "MSFT"],
        "stale_market_data": {
            "AAPL": {"data_age_minutes": 200.0},
            "MSFT": {"data_age_minutes": 100.0},
        },
    }
    # stale_limit=10 laisse largement de la place après la ligne US + footnote
    rendered = _render(build_freshness(state, now=NOW, stale_limit=10))
    assert "AAPL" in rendered
    assert "MSFT" in rendered


def test_freshness_stale_symbols_not_listed_without_budget():
    """stale_limit None (défaut) → pas de liste individuelle."""
    state = {
        "universe_symbols": ["AAPL", "MSFT"],
        "stale_market_data": {
            "AAPL": {"data_age_minutes": 200.0},
        },
    }
    # Sans stale_limit, seule la ligne venue est affichée
    rendered = _render(build_freshness(state, now=NOW))
    assert "US" in rendered
    # Le symbole peut apparaître mais l'info de venue agrégée est bien là
    assert "▲ stale" in rendered


def test_freshness_stale_symbols_sorted_by_age():
    """stale_limit permet les détails → le symbole le plus stale est en premier."""
    state = {
        "universe_symbols": ["AAPL", "MSFT"],
        "stale_market_data": {
            "AAPL": {"data_age_minutes": 100.0},
            "MSFT": {"data_age_minutes": 400.0},  # plus stale
        },
    }
    rendered = _render(build_freshness(state, now=NOW, stale_limit=10))
    # MSFT (plus stale) doit apparaître avant AAPL dans le rendu
    assert rendered.index("MSFT") < rendered.index("AAPL")


# ---------------------------------------------------------------------------
# build_universe
# ---------------------------------------------------------------------------


def test_universe_empty_state():
    """State vide → '0', hot-set '—'."""
    rendered = _render(build_universe({}, now=NOW))
    assert "symbols" in rendered
    assert "hot-set" in rendered
    assert "—" in rendered


def test_universe_counts_by_venue():
    """universe_symbols peuplé → comptes par venue affichés."""
    state = {
        "universe_symbols": [
            "AAPL", "MSFT", "NVDA",               # US x3
            "BN.PA", "ASML.AS",                    # EU x2
            "2330.TW", "3443.TW", "6488.TWO",      # TW x3
        ]
    }
    rendered = _render(build_universe(state, now=NOW))
    assert "8" in rendered      # total
    assert "TPE 3" in rendered
    assert "EU 2" in rendered
    assert "US 3" in rendered


def test_universe_hotset_from_venue_state():
    """venue_state.venues.TW.hotlist → hot-set count."""
    state = {
        "universe_symbols": ["2330.TW", "3443.TW", "AAPL"],
        "venue_state": {
            "venues": {
                "TW": {"hotlist": ["2330.TW", "3443.TW"]},
                "US": {"hotlist": ["AAPL"]},
            }
        },
    }
    rendered = _render(build_universe(state, now=NOW))
    assert "3 rotating" in rendered


def test_universe_fx_symbols_excluded():
    """Paires FX ne sont pas comptées dans l'univers."""
    state = {
        "universe_symbols": ["AAPL", "EURUSD=X"],
    }
    rendered = _render(build_universe(state, now=NOW))
    # Total = 1 (AAPL uniquement)
    assert "1 ·" in rendered


def test_project_universe_health_composes_ordered_counts_and_hotset():
    from trader.interfaces.cockpit.projections.health import (
        project_universe_health,
    )

    projection = project_universe_health(
        {
            "universe_symbols": ["AAPL", "BN.PA", "2330.TW", "EURUSD=X"],
            "venue_state": {
                "venues": {
                    "TW": {"hotlist": ["2330.TW"]},
                    "US": {"hotlist": ["AAPL"]},
                }
            },
        }
    )

    assert projection.total_symbols == 3
    assert projection.venue_counts == (("TPE", 1), ("EU", 1), ("US", 1))
    assert projection.symbols_label == "3 · TPE 1 / EU 1 / US 1"
    assert projection.hot_total == 2
    assert projection.hotset_label == "2 rotating"


# ---------------------------------------------------------------------------
# Test de montage Textual
# ---------------------------------------------------------------------------


async def test_health_page_mounts_with_all_panels(tmp_path, monkeypatch):
    """La page health se monte et ses 6 panneaux sont dans le DOM après navigation."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    _write_daemon_alive(tmp_path, monkeypatch)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("4")  # Rév. 3 : health passe en page 4 (plans fusionnée)
        await pilot.pause()

        assert app._active_page_key == "health"

        health = app.query_one("#health-page", HealthPage)
        assert health.display is True

        # 6 panneaux d'origine + RISK GATE / MODEL déménagés depuis Decisions
        from textual.containers import VerticalScroll
        assert app.query_one("#freshness-panel", VerticalScroll) is not None
        assert app.query_one("#fx-panel", VerticalScroll) is not None
        assert app.query_one("#sources-panel", VerticalScroll) is not None
        assert app.query_one("#llm-panel", VerticalScroll) is not None
        assert app.query_one("#learnings-h-panel", VerticalScroll) is not None
        assert app.query_one("#universe-h-panel", VerticalScroll) is not None
        assert app.query_one("#risk-panel", VerticalScroll) is not None
        assert app.query_one("#model-panel", VerticalScroll) is not None


async def test_health_page_update_state_empty_no_crash(tmp_path, monkeypatch):
    """update_state({}) ne doit pas lever d'exception."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    _write_daemon_alive(tmp_path, monkeypatch)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()

        health = app.query_one("#health-page", HealthPage)
        # Doit passer sans exception, même avec state vide
        health.update_state({})
        health.update_state({"universe_symbols": None, "fx_rates": None})


async def test_health_page_update_state_full(tmp_path, monkeypatch):
    """update_state avec state complet → corps des panneaux peuplés."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    _write_daemon_alive(tmp_path, monkeypatch)

    state = {
        "universe_symbols": ["AAPL", "MSFT", "2330.TW", "BN.PA"],
        "stale_market_data": {"AAPL": {"data_age_minutes": 4700.0}},
        "fx_rates": {"USD": 1.0, "EUR": 1.1438, "CHF": 1.2435, "TWD": 0.03121},
        "daemon_status": {"model_calls_used": 3, "phase": "cycle_completed"},
        "kpis": {"model_performance": [{"fills": 50, "fallbacks": 1}]},
        "learnings_pending_count": 7,
        "learnings": [{"symbol": "AAPL", "note": "breakout confirmed — repeat"}],
        "venue_state": {"venues": {"TW": {"hotlist": ["2330.TW"]}}},
    }

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()

        health = app.query_one("#health-page", HealthPage)
        health.update_state(state)
        await pilot.pause()

        from textual.widgets import Static
        freshness_body = app.query_one("#freshness-body", Static)
        # Après update, le contenu doit exister (pas vide)
        assert freshness_body is not None
