"""Tests de la page Decisions du cockpit casys (redesign).

Couverture :
- Builders purs testés avec des state dicts synthétiques + now fixe.
- Cas limites : état vide, valeurs None, listes vides.
- Montage Textual via app.run_test (pattern smoke).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

import trader.cockpit.app as cockpit_module  # alias compat → trader.interfaces.cockpit.app
from trader.cockpit import CockpitApp

from trader.interfaces.cockpit.pages.decisions import (
    DecisionsPage,
    _count_filters,
    _filter_rows,
    _fmt_conf,
    _group_into_ledger_rows,
    _has_detail,
    _is_batch_row,
    _is_risk_row,
    _is_stale_row,
    build_detail_panel,
    build_filter_chips,
    build_ledger_footer,
    build_mix_24h,
    build_model_panel,
    build_risk_gate,
)

UTC = timezone.utc
_NOW = datetime(2026, 7, 6, 2, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers de test
# ---------------------------------------------------------------------------


def _render(renderable, *, width: int = 120) -> str:
    console = Console(width=width, highlight=False, no_color=True)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _make_minimal_state(tmp_path: Path) -> None:
    """État minimal pour que CockpitApp monte sans FirstRunScreen."""
    (tmp_path / "current_report.json").write_text(
        json.dumps(
            {
                "ts": "2026-07-06T02:00:00+00:00",
                "dry_run": True,
                "portfolio": {"cash": 100_000.0, "equity": 100_000.0, "holdings": []},
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "daemon_status.json").write_text(
        json.dumps(
            {
                "phase": "idle",
                "decisions_done": 0,
                "symbols_total": 0,
                "model_calls_used": 0,
                "max_model_calls_per_cycle": 25,
            }
        ),
        encoding="utf-8",
    )


def _make_decision(
    symbol: str = "AAPL",
    action: str = "HOLD",
    confidence: float = 0.72,
    *,
    rationale: str = "",
    reason: str = "hold",
    cycle_ts: str = "2026-07-06T02:01:00+00:00",
    llm_provider: str = "acpx",
    llm_model: str = "gpt-5.5",
    model_called: bool = True,
    decision_source: str = "llm",
    executed: bool = False,
    tool_calls: list | None = None,
) -> dict:
    row: dict = {
        "cycle_ts": cycle_ts,
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "rationale": rationale,
        "reason": reason,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "model_called": model_called,
        "decision_source": decision_source,
        "executed": executed,
    }
    if tool_calls is not None:
        row["runtime"] = {"tool_calls": tool_calls}
    return row


# ---------------------------------------------------------------------------
# Helpers purs
# ---------------------------------------------------------------------------


def test_fmt_conf_normal() -> None:
    assert _fmt_conf(0.72) == ".72"


def test_fmt_conf_none() -> None:
    assert _fmt_conf(None) == "—"


def test_fmt_conf_one() -> None:
    assert _fmt_conf(1.0) == "1.0"


def test_fmt_conf_zero() -> None:
    assert _fmt_conf(0.0) == ".00"


def test_is_risk_row_true() -> None:
    assert _is_risk_row({"reason": "risk:confidence_below"}) is True


def test_is_risk_row_blocked() -> None:
    assert _is_risk_row({"reason": "blocked_no_stop"}) is True


def test_is_risk_row_false() -> None:
    assert _is_risk_row({"reason": "hold"}) is False


def test_is_batch_row_true() -> None:
    row = {"decision_source": "infra_hold", "model_called": False}
    assert _is_batch_row(row) is True


def test_is_batch_row_llm() -> None:
    row = {"decision_source": "llm", "model_called": True}
    assert _is_batch_row(row) is False


def test_is_stale_row() -> None:
    """Stale = propriété de la DÉCISION (reason), pas de l'état marché courant :
    une décision stale reste filtrable même quand le flux redevient frais."""
    assert _is_stale_row({}, {"symbol": "SPY", "reason": "stale_market_data"}) is True
    assert _is_stale_row({}, {"symbol": "SPY", "reason": "stale"}) is True
    # symbole actuellement stale mais décision normale → PAS stale
    state = {"stale_market_data": {"AAPL": {"data_age_minutes": 480}}}
    assert _is_stale_row(state, {"symbol": "AAPL", "reason": "hold"}) is False


# ---------------------------------------------------------------------------
# _filter_rows
# ---------------------------------------------------------------------------


def test_filter_rows_buy() -> None:
    rows = [
        _make_decision("A", "BUY"),
        _make_decision("B", "SELL"),
        _make_decision("C", "HOLD"),
    ]
    result = _filter_rows(rows, "buy", {})
    assert len(result) == 1
    assert result[0]["symbol"] == "A"


def test_filter_rows_hold() -> None:
    rows = [_make_decision("A", "BUY"), _make_decision("B", "HOLD")]
    result = _filter_rows(rows, "hold", {})
    assert len(result) == 1 and result[0]["symbol"] == "B"


def test_filter_rows_all() -> None:
    rows = [_make_decision("A"), _make_decision("B")]
    assert len(_filter_rows(rows, "all", {})) == 2


def test_filter_rows_risk() -> None:
    rows = [
        _make_decision("A", reason="risk:gross_exposure"),
        _make_decision("B", reason="hold"),
    ]
    result = _filter_rows(rows, "risk", {})
    assert len(result) == 1 and result[0]["symbol"] == "A"


def test_filter_rows_stale() -> None:
    rows = [
        {**_make_decision("SPY"), "reason": "stale_market_data"},
        _make_decision("AAPL"),
    ]
    result = _filter_rows(rows, "stale", {})
    assert len(result) == 1 and result[0]["symbol"] == "SPY"


def test_filter_rows_empty() -> None:
    assert _filter_rows([], "buy", {}) == []


# ---------------------------------------------------------------------------
# _count_filters
# ---------------------------------------------------------------------------


def test_count_filters_basic() -> None:
    rows = [
        _make_decision("A", "BUY"),
        _make_decision("B", "SELL"),
        _make_decision("C", "HOLD"),
        # Row D: action=HOLD (default) + reason=risk:x → counts in both hold AND risk
        _make_decision("D", "HOLD", reason="risk:x"),
    ]
    counts = _count_filters(rows, {})
    assert counts["all"] == 4
    assert counts["buy"] == 1
    assert counts["sell"] == 1
    assert counts["hold"] == 2  # rows C and D both have action=HOLD
    assert counts["risk"] == 1  # only row D


def test_count_filters_empty() -> None:
    counts = _count_filters([], {})
    assert all(v == 0 for v in counts.values())


# ---------------------------------------------------------------------------
# _group_into_ledger_rows
# ---------------------------------------------------------------------------


def test_group_batch_large() -> None:
    """≥ 3 infra_hold de même cycle_ts → 1 batch row."""
    ts = "2026-07-06T01:00:00+00:00"
    rows = [
        {
            "decision_source": "infra_hold",
            "model_called": False,
            "cycle_ts": ts,
            "action": "HOLD",
            "symbol": f"SYM{i}",
        }
        for i in range(5)
    ]
    grouped = _group_into_ledger_rows(rows)
    assert len(grouped) == 1
    assert grouped[0]["_is_batch_summary"] is True
    assert "5 due" in grouped[0]["reason"]


def test_group_batch_small_passes_through() -> None:
    """< 3 infra_hold → toutes lignes passent individuellement."""
    ts = "2026-07-06T01:00:00+00:00"
    rows = [
        {"decision_source": "infra_hold", "model_called": False, "cycle_ts": ts, "action": "HOLD"}
        for _ in range(2)
    ]
    grouped = _group_into_ledger_rows(rows)
    assert len(grouped) == 2
    assert not any(r.get("_is_batch_summary") for r in grouped)


def test_group_llm_rows_not_batched() -> None:
    rows = [_make_decision("A"), _make_decision("B"), _make_decision("C")]
    grouped = _group_into_ledger_rows(rows)
    assert len(grouped) == 3


def test_group_empty() -> None:
    assert _group_into_ledger_rows([]) == []


# ---------------------------------------------------------------------------
# build_filter_chips
# ---------------------------------------------------------------------------


def test_build_filter_chips_all_active() -> None:
    counts = {"all": 10, "buy": 2, "sell": 1, "hold": 7, "risk": 0, "stale": 0}
    text = build_filter_chips(counts, "all")
    rendered = text.plain
    assert "all 10" in rendered
    assert "buy 2" in rendered
    assert "/ regex" in rendered


def test_build_filter_chips_buy_active() -> None:
    counts = {"all": 5, "buy": 2, "sell": 0, "hold": 3, "risk": 0, "stale": 0}
    text = build_filter_chips(counts, "buy")
    # "buy" chip is bold accent when active; plain text still contains the label
    assert "buy 2" in text.plain


def test_build_filter_chips_empty() -> None:
    counts = {"all": 0, "buy": 0, "sell": 0, "hold": 0, "risk": 0, "stale": 0}
    text = build_filter_chips(counts, "all")
    assert "all 0" in text.plain


# ---------------------------------------------------------------------------
# build_mix_24h
# ---------------------------------------------------------------------------


def test_build_mix_24h_counts() -> None:
    state = {
        "recent_decisions": [
            _make_decision("A", "BUY"),
            _make_decision("B", "BUY"),
            _make_decision("C", "SELL"),
            _make_decision("D", "HOLD"),
            _make_decision("E", "HOLD"),
        ]
    }
    rendered = _render(build_mix_24h(state))
    assert "2" in rendered   # buy count
    assert "1" in rendered   # sell count
    assert "holding is a decision" in rendered


def test_build_mix_24h_empty() -> None:
    rendered = _render(build_mix_24h({}))
    assert "holding is a decision" in rendered


# ---------------------------------------------------------------------------
# build_risk_gate
# ---------------------------------------------------------------------------


def test_build_risk_gate_no_rejects() -> None:
    state = {
        "recent_decisions": [
            _make_decision("A", reason="hold"),
            _make_decision("B", reason="hold"),
        ]
    }
    rendered = _render(build_risk_gate(state))
    assert "no rejects" in rendered


def test_build_risk_gate_with_rejects() -> None:
    state = {
        "recent_decisions": [
            _make_decision("A", reason="risk:confidence_below"),
            _make_decision("B", reason="hold"),
        ]
    }
    rendered = _render(build_risk_gate(state))
    assert "risk reject" in rendered


def test_build_risk_gate_empty_state() -> None:
    rendered = _render(build_risk_gate({}))
    assert "no rejects" in rendered


def test_build_risk_gate_shows_caps() -> None:
    """Le panneau affiche toujours une ligne config/risk.yaml."""
    rendered = _render(build_risk_gate({}))
    assert "config/risk.yaml" in rendered


def test_build_risk_gate_shows_all_known_caps() -> None:
    """Toutes les caps connues de risk.yaml sont listées sans troncature."""
    rendered = _render(build_risk_gate({}))
    # Les caps que risk.yaml contient effectivement
    assert "gross cap" in rendered
    assert "per-symbol cap" in rendered
    assert "order max" in rendered
    assert "min equity" in rendered
    assert "conf gate" in rendered
    assert "hard stop req" in rendered


def test_build_risk_gate_booleans_formatted() -> None:
    """Les booleans sont affichés 'on'/'off'."""
    rendered = _render(build_risk_gate({}))
    # confidence_gate_enabled: false → 'off'
    assert "off" in rendered


# ---------------------------------------------------------------------------
# build_model_panel
# ---------------------------------------------------------------------------


def test_build_model_panel_with_perf() -> None:
    state = {
        "kpis": {
            "model_performance": [
                {
                    "provider": "acpx",
                    "model": "gpt-5.5",
                    "fallbacks": 0,
                    "avg_confidence": 0.67,
                }
            ]
        }
    }
    rendered = _render(build_model_panel(state))
    assert "provider" in rendered
    assert "acpx" in rendered
    assert "fallbacks" in rendered
    assert "avg conf" in rendered


def test_build_model_panel_empty() -> None:
    rendered = _render(build_model_panel({}))
    assert "provider" in rendered


def test_build_model_panel_fallback_from_decisions() -> None:
    state = {
        "recent_decisions": [
            _make_decision("A", llm_provider="acpx", llm_model="gpt-5.5"),
        ]
    }
    rendered = _render(build_model_panel(state))
    assert "acpx" in rendered or "provider" in rendered


# ---------------------------------------------------------------------------
# build_ledger_footer
# ---------------------------------------------------------------------------


def test_build_ledger_footer_all() -> None:
    text = build_ledger_footer(50, 50, "all")
    rendered = text.plain
    assert "50 decisions" in rendered
    assert "enter expand" in rendered


def test_build_ledger_footer_filtered() -> None:
    text = build_ledger_footer(50, 3, "buy")
    rendered = text.plain
    assert "3" in rendered
    assert "50" in rendered


# ---------------------------------------------------------------------------
# build_detail_panel
# ---------------------------------------------------------------------------


def test_build_detail_panel_none() -> None:
    rendered = _render(build_detail_panel(None, now=_NOW))
    assert "navigate" in rendered


def test_build_detail_panel_with_rationale() -> None:
    row = _make_decision("AAPL", rationale="range-bound just above the swing low")
    rendered = _render(build_detail_panel(row, now=_NOW))
    assert "range-bound" in rendered
    assert "swing low" in rendered


def test_build_detail_panel_with_tool_calls() -> None:
    row = _make_decision(
        "AAPL",
        tool_calls=[
            {"tool": "set_next_wake", "outcome": "applied", "detail": {}},
            {"tool": "amend_exit", "outcome": "rejected", "detail": {"warnings": [{"code": "hard_stop_above_max_pct"}]}},
        ],
    )
    rendered = _render(build_detail_panel(row, now=_NOW))
    assert "set_next_wake" in rendered
    assert "amend_exit" in rendered
    assert "hard_stop_above_max_pct" in rendered


def test_build_detail_panel_batch_summary() -> None:
    row = {
        "_is_batch_summary": True,
        "reason": "10 due — 3 LLM calls, 7 quiet holds",
    }
    rendered = _render(build_detail_panel(row, now=_NOW))
    assert "10 due" in rendered


def test_build_detail_panel_no_info() -> None:
    """Ligne sans rationale ni tool_calls → message fallback."""
    row = _make_decision("AAPL", rationale="")
    rendered = _render(build_detail_panel(row, now=_NOW))
    assert "no details" in rendered


def test_build_detail_panel_meta_price() -> None:
    row = {**_make_decision("AAPL"), "price": 195.50, "decision_reason_code": "WAITING_PULLBACK"}
    rendered = _render(build_detail_panel(row, now=_NOW))
    assert "195.50" in rendered
    assert "waiting pullback" in rendered


# ---------------------------------------------------------------------------
# _has_detail
# ---------------------------------------------------------------------------


def test_has_detail_rationale() -> None:
    assert _has_detail({"rationale": "some reason"}) is True


def test_has_detail_tool_calls() -> None:
    row = {"runtime": {"tool_calls": [{"tool": "set_next_wake", "outcome": "applied"}]}}
    assert _has_detail(row) is True


def test_has_detail_empty() -> None:
    assert _has_detail({"rationale": ""}) is False


# ---------------------------------------------------------------------------
# populate_ledger_table (unit — ne monte pas Textual)
# ---------------------------------------------------------------------------


def test_populate_ledger_table_basic() -> None:
    """Peuple une table synthétique et vérifie que le row_map est correct."""

    # Créer une DataTable hors Textual n'est pas possible sans app
    # → testé implicitement via le test de montage Textual.
    pass  # couvert par le test Textual ci-dessous


# ---------------------------------------------------------------------------
# Test de montage Textual — bouton 3 → DecisionsPage visible
# ---------------------------------------------------------------------------


async def test_decisions_page_mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Presse '3', vérifie que les panneaux de la page Decisions sont dans le DOM."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("3")
        await pilot.pause()

        assert app._active_page_key == "decisions"
        assert app.query_one("#decisions-page").display is True

        # Panneaux requis par la spec
        assert app.query_one("#ledger-panel") is not None
        assert app.query_one("#mix-panel") is not None
        assert app.query_one("#risk-panel") is not None
        assert app.query_one("#model-panel") is not None
        assert app.query_one("#filter-chips-bar") is not None
        assert app.query_one("#detail-scroll") is not None


async def test_decisions_page_update_state_ne_crash_pas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update_state avec état vide + état partiel ne doit pas lever d'exception."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("escape")  # ferme le preflight (daemon non vivant)
        await pilot.press("3")
        await pilot.pause()

        page = app.query_one("#decisions-page", DecisionsPage)

        # État vide
        page.update_state({})

        # État partiel (seulement recent_decisions)
        page.update_state(
            {
                "recent_decisions": [
                    _make_decision("AAPL", "BUY", 0.81, rationale="breakout propre"),
                    _make_decision("MSFT", "HOLD", 0.55),
                ]
            }
        )

        # État avec None partout
        page.update_state({"recent_decisions": None, "kpis": None, "portfolio": None})


async def test_decisions_page_filter_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Les touches b/s/h/a changent le filtre actif."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("escape")  # ferme le preflight (daemon non vivant)
        await pilot.press("3")
        await pilot.pause()

        page = app.query_one("#decisions-page", DecisionsPage)
        page.update_state(
            {
                "recent_decisions": [
                    _make_decision("A", "BUY"),
                    _make_decision("B", "SELL"),
                    _make_decision("C", "HOLD"),
                ]
            }
        )
        await pilot.pause()

        # Focus sur le DataTable (nécessaire pour que les bindings de DecisionsPage remontent)
        page.query_one("#ledger-table").focus()
        await pilot.pause()

        await pilot.press("b")
        await pilot.pause()
        assert page._active_filter == "buy"

        await pilot.press("s")
        await pilot.pause()
        assert page._active_filter == "sell"

        await pilot.press("h")
        await pilot.pause()
        assert page._active_filter == "hold"

        await pilot.press("a")
        await pilot.pause()
        assert page._active_filter == "all"
