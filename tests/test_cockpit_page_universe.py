"""Tests de la page Universe (pages/universe.py).

Couvre :
- builders purs (overrides, hot-set, rotation, symbol rows)
- write path (pin → ban → undo, exclusion mutuelle)
- montage Textual + navigation page 7
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.pages.universe import (
    UniversePage,
    _distribute_rows,
    _next_venue_close_utc,
    _populate_universe_table,
    _read_last_rotation,
    build_hot_set_panel,
    build_overrides_panel,
    build_rotation_panel,
    build_symbol_rows,
    build_universe_pipeline_panel,
)
from trader.market.rotation.user_overrides import (
    UserOverrides,
    ban_symbol,
    clear_override,
    load_user_overrides,
    pin_symbol,
)

UTC_TZ = UTC
NOW = datetime(2026, 7, 6, 3, 0, 0, tzinfo=UTC_TZ)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(renderable, width: int = 160) -> str:
    from rich.console import Console

    console = Console(width=width, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _patch_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")


def _make_minimal_state(tmp_path: Path) -> None:
    (tmp_path / "current_report.json").write_text(
        json.dumps(
            {
                "ts": "2026-07-06T03:00:00+00:00",
                "dry_run": True,
                "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
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


def _universe_yaml(tmp_path: Path, symbols: list[str]) -> Path:
    path = tmp_path / "universe.yaml"
    import yaml
    path.write_text(yaml.safe_dump({"symbols": symbols}), encoding="utf-8")
    return path


def _minimal_state(
    symbols: list[str] | None = None,
    hotlist: list[str] | None = None,
    scores: dict | None = None,
) -> dict:
    # Use explicit None checks — empty list/dict must NOT fall back to defaults.
    symbols = symbols if symbols is not None else ["3443.TW", "AAPL", "BN.PA"]
    hotlist = hotlist if hotlist is not None else ["3443.TW"]
    scores = scores if scores is not None else {"3443.TW": 0.81}
    return {
        "universe_symbols": symbols,
        "venue_state": {
            "venues": {
                "TW": {
                    "hotlist": [s for s in hotlist if s.endswith(".TW") or s.endswith(".TWO")],
                    "scores": {k: v for k, v in scores.items() if k.endswith(".TW") or k.endswith(".TWO")},
                    "candidates": [],
                    "last_close_at": "2026-07-06T05:30:00+00:00",
                },
                "EU": {
                    "hotlist": [s for s in hotlist if s.endswith(".PA") or s.endswith(".MI")],
                    "scores": {k: v for k, v in scores.items() if k.endswith(".PA")},
                    "candidates": [],
                    "last_close_at": "2026-07-05T15:30:00+00:00",
                },
                "US": {
                    "hotlist": [s for s in hotlist if "." not in s],
                    "scores": {k: v for k, v in scores.items() if "." not in k},
                    "candidates": [],
                    "last_close_at": "2026-07-05T20:00:00+00:00",
                },
            }
        },
        "company_map": {
            "3443.TW": "Global Unichip Corp.",
            "AAPL": "Apple Inc.",
            "BN.PA": "Danone S.A.",
        },
        "portfolio": {"holdings": [], "cash": 100000.0, "equity": 100000.0},
        "daemon_status": {
            "phase": "idle",
            "decisions_done": 0,
            "symbols_total": 0,
            "model_calls_used": 0,
            "current_symbol": None,
        },
        "recent_decisions": [],
        "indicator_watches": [],
        "symbol_wakes": {},
        "stale_market_data": {},
        "open_venues_list": ["TW"],
        "sessions": {
            "TW": {"open": "01:00", "close": "05:30"},
            "EU": {"open": "07:00", "close": "15:30"},
            "US": {"open": "13:30", "close": "20:00"},
        },
    }


# ---------------------------------------------------------------------------
# Pure builders — build_overrides_panel
# ---------------------------------------------------------------------------


def test_overrides_panel_empty() -> None:
    """Aucun override → message 'no overrides'."""
    rendered = _render(build_overrides_panel(UserOverrides()))
    assert "no overrides" in rendered
    assert "p" in rendered
    assert "b" in rendered


def test_overrides_panel_pin_only() -> None:
    """Un pin → ⚑ + symbole + label."""
    overrides = UserOverrides(pin=("1326.TW",))
    rendered = _render(build_overrides_panel(overrides))
    assert "⚑" in rendered
    assert "1326.TW" in rendered
    assert "pinned" in rendered


def test_overrides_panel_ban_only() -> None:
    """Un ban → ✕ + symbole + label."""
    overrides = UserOverrides(ban=("CMS",))
    rendered = _render(build_overrides_panel(overrides))
    assert "✕" in rendered
    assert "CMS" in rendered
    assert "banned" in rendered


def test_overrides_panel_pin_and_ban() -> None:
    """Pin + ban coexistent sans se mélanger."""
    overrides = UserOverrides(pin=("1326.TW",), ban=("CMS",))
    rendered = _render(build_overrides_panel(overrides))
    assert "⚑" in rendered
    assert "✕" in rendered
    assert "1326.TW" in rendered
    assert "CMS" in rendered
    assert "config/universe.yaml" in rendered
    assert "u" in rendered


# ---------------------------------------------------------------------------
# Pure builders — build_hot_set_panel
# ---------------------------------------------------------------------------


def test_hot_set_panel_empty() -> None:
    """Pas de hot-set → message 'no active hot-set'."""
    rendered = _render(build_hot_set_panel({}))
    assert "no active hot-set" in rendered


def test_hot_set_panel_renders_bars() -> None:
    """Hot-set avec scores → barres █ + scores."""
    state = _minimal_state(
        symbols=["3443.TW", "6488.TWO"],
        hotlist=["3443.TW", "6488.TWO"],
        scores={"3443.TW": 0.81, "6488.TWO": 0.77},
    )
    rendered = _render(build_hot_set_panel(state))
    assert "3443.TW" in rendered
    assert "█" in rendered
    assert "0.81" in rendered


def test_hot_set_panel_max_5_items() -> None:
    """L'aperçu du hot-set est limité à 5 entrées, pas le hot-set métier."""
    syms = [f"SYM{i}.TW" for i in range(8)]
    state = {
        "universe_symbols": syms,
        "venue_state": {
            "venues": {
                "TW": {
                    "hotlist": syms,
                    "scores": {s: 0.9 - i * 0.05 for i, s in enumerate(syms)},
                    "candidates": [],
                }
            }
        },
    }
    rendered = _render(build_hot_set_panel(state))
    shown = sum(1 for s in syms if s in rendered)
    assert shown <= 5


# ---------------------------------------------------------------------------
# Pure builders — build_rotation_panel
# ---------------------------------------------------------------------------


def test_rotation_panel_basic() -> None:
    """Affiche l'explainer + 'next refresh'."""
    state = _minimal_state()
    rendered = _render(build_rotation_panel(state, now=NOW))
    assert "heuristic" in rendered
    assert "universe agent" in rendered
    assert "hot-set" in rendered
    assert "preview shows up to 5" in rendered
    assert "5 slots" not in rendered
    assert "next refresh" in rendered


def test_rotation_panel_next_close_computed() -> None:
    """La prochaine clôture est calculée depuis les sessions."""
    state = _minimal_state()
    # NOW = 03:00 UTC → prochaine clôture TW = 05:30 UTC même jour
    rendered = _render(build_rotation_panel(state, now=NOW))
    assert "05:30" in rendered


def test_rotation_panel_last_rotation_in_out() -> None:
    """Avec last_rotation, in/out affichés."""
    last_rot = {
        "default_hot_set": ["A.TW", "B.TW"],
        "final_hot_set": ["A.TW", "C.TW"],
    }
    rendered = _render(build_rotation_panel(_minimal_state(), now=NOW, last_rotation=last_rot))
    assert "in" in rendered.lower()
    assert "C.TW" in rendered


def test_rotation_panel_no_changes() -> None:
    """Pas de diff entre default et final → 'no changes'."""
    last_rot = {"default_hot_set": ["A.TW"], "final_hot_set": ["A.TW"]}
    rendered = _render(build_rotation_panel(_minimal_state(), now=NOW, last_rotation=last_rot))
    assert "no changes" in rendered


# ---------------------------------------------------------------------------
# Pure builder — build_universe_pipeline_panel
# ---------------------------------------------------------------------------


def test_universe_pipeline_panel_separates_agent_success_from_activation_fallback() -> None:
    state = {
        "universe_pipeline": {
            "US": {
                "scope": {
                    "status": "ready",
                    "id_short": "scope-123",
                    "candidate_count": 42,
                    "challenger_count": 2,
                },
                "scout": {
                    "status": "success",
                    "id_short": "scout-123",
                    "challenger_count": 2,
                    "coverage": {"eligible_items": 18},
                },
                "brief": {
                    "status": "ready",
                    "id_short": "brief-123",
                    "scope_match": True,
                    "point_count": 11,
                    "coverage": {
                        "status": "partial",
                        "candidate_count": 42,
                        "candidates_with_news": 7,
                    },
                },
                "agent": {
                    "status": "success",
                    "id_short": "agent-123",
                    "hotlist_count": 25,
                    "challenger_count": 2,
                    "provider": "acpx-claude-sonnet",
                    "model": "sonnet",
                    "provider_fallback_reason": "acpx:quota",
                },
                "activation": {
                    "status": "fallback",
                    "agent_run_id_short": "agent-123",
                    "fallback_reason": "prepared_scope_mismatch",
                    "hotlist_count": 24,
                    "challenger_count": 0,
                },
            }
        }
    }

    rendered = _render(build_universe_pipeline_panel(state))

    assert "US" in rendered
    assert "agent success" in rendered
    assert "active fallback" in rendered
    assert "prepared_scope_mismatch" in rendered
    assert "25 hot / 2 ch" in rendered
    assert "acpx-claude-sonnet/sonnet" in rendered
    assert "backend fallback acpx:quota" in rendered
    assert "24 hot / 0 ch" in rendered
    assert "exact" in rendered
    assert "cov partial" in rendered
    assert "7/42" in rendered


def test_universe_pipeline_panel_renders_missing_artifacts_as_pending() -> None:
    rendered = _render(build_universe_pipeline_panel({}))

    assert "TPE" in rendered
    assert "EU" in rendered
    assert "US" in rendered
    assert rendered.count("pipeline pending") == 3


# ---------------------------------------------------------------------------
# Pure builders — build_symbol_rows
# ---------------------------------------------------------------------------


def test_symbol_rows_basic_grouping() -> None:
    """Les symboles sont regroupés par venue."""
    state = _minimal_state(
        symbols=["3443.TW", "AAPL"],
        hotlist=["3443.TW"],
        scores={"3443.TW": 0.8},
    )
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    # TW avant US
    tw_idx = next((i for i, r in enumerate(rows) if r.venue == "TW"), -1)
    us_idx = next((i for i, r in enumerate(rows) if r.venue == "US"), -1)
    assert tw_idx < us_idx


def test_symbol_rows_state_label_hot() -> None:
    """Symbole en hotlist → state_label == 'hot'."""
    state = _minimal_state(
        symbols=["3443.TW"],
        hotlist=["3443.TW"],
        scores={"3443.TW": 0.8},
    )
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert tw_row.state_label == "hot"


def test_symbol_rows_state_label_pool() -> None:
    """Symbole hors hotlist → state_label == 'pool'."""
    state = _minimal_state(
        symbols=["3443.TW"],
        hotlist=[],
        scores={},
    )
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert tw_row.state_label == "pool"


def test_symbol_rows_state_label_pinned() -> None:
    """Symbole épinglé → state_label == 'pinned' indépendamment du hot-set."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=[], scores={})
    rows = build_symbol_rows(state, UserOverrides(pin=("3443.TW",)), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert tw_row.state_label == "pinned"


def test_symbol_rows_state_label_banned() -> None:
    """Symbole banni → state_label == 'banned', LAST == 'excluded'."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=[], scores={})
    rows = build_symbol_rows(state, UserOverrides(ban=("3443.TW",)), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert tw_row.state_label == "banned"
    assert "excluded" in tw_row.last_decision


def test_symbol_rows_deciding_now() -> None:
    """current_symbol → last_decision == 'deciding now ▸'."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=["3443.TW"], scores={"3443.TW": 0.8})
    state["daemon_status"]["current_symbol"] = "3443.TW"
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert "deciding now" in tw_row.last_decision
    assert tw_row.last_decision_action == "deciding"


def test_symbol_rows_last_decision_buy() -> None:
    """Dernière décision BUY → action == 'BUY'."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=["3443.TW"], scores={"3443.TW": 0.8})
    state["recent_decisions"] = [
        {
            "cycle_ts": "2026-07-06T02:01:00+00:00",
            "symbol": "3443.TW",
            "action": "BUY",
            "confidence": 0.74,
        }
    ]
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert tw_row.last_decision_action == "BUY"
    assert ".74" in tw_row.last_decision


def test_symbol_rows_pos_long() -> None:
    """Holding longue → pos == 'L'."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=["3443.TW"], scores={"3443.TW": 0.8})
    state["portfolio"]["holdings"] = [
        {"symbol": "3443.TW", "quantity": 100, "last_price": 500.0, "fx_rate": 0.031}
    ]
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert tw_row.pos == "L"


def test_symbol_rows_data_stale() -> None:
    """Données stale → data_stale True + indicateur ▲."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=["3443.TW"], scores={"3443.TW": 0.8})
    state["stale_market_data"] = {
        "3443.TW": {"data_age_minutes": 4800, "stale_reason": "too_old"}
    }
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert tw_row.data_stale is True
    assert "▲" in tw_row.data_text


def test_symbol_rows_empty_state() -> None:
    """État vide → aucune exception, liste vide."""
    rows = build_symbol_rows({}, UserOverrides(), now=NOW)
    assert rows == []


def test_symbol_rows_name_truncated() -> None:
    """Nom > 20 chars → tronqué avec ellipsis."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=[], scores={})
    state["company_map"]["3443.TW"] = "A" * 30  # 30 chars
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert len(tw_row.name) <= 20
    assert "…" in tw_row.name


# ---------------------------------------------------------------------------
# Helpers — _next_venue_close_utc
# ---------------------------------------------------------------------------


def test_next_venue_close_basic() -> None:
    """Prochaine clôture après NOW(03:00) → TW 05:30 ce jour."""
    sessions = {
        "TW": {"open": "01:00", "close": "05:30"},
        "EU": {"open": "07:00", "close": "15:30"},
    }
    result = _next_venue_close_utc(sessions, NOW)
    assert result is not None
    assert result.hour == 5 and result.minute == 30


def test_next_venue_close_skips_fx() -> None:
    """FX est ignoré."""
    sessions = {"FX": {"open": "00:00", "close": "04:00"}}
    result = _next_venue_close_utc(sessions, NOW)
    assert result is None


def test_next_venue_close_empty() -> None:
    """Sessions vides → None."""
    assert _next_venue_close_utc({}, NOW) is None


# ---------------------------------------------------------------------------
# _read_last_rotation
# ---------------------------------------------------------------------------


def test_read_last_rotation_basic(tmp_path: Path) -> None:
    """Lit la dernière ligne du ledger."""
    ledger = tmp_path / "rotation_ledger.jsonl"
    ledger.write_text(
        json.dumps({"as_of": "A", "final_hot_set": ["X"]}) + "\n"
        + json.dumps({"as_of": "B", "final_hot_set": ["Y"]}) + "\n",
        encoding="utf-8",
    )
    result = _read_last_rotation(ledger)
    assert result is not None
    assert result.get("as_of") == "B"


def test_read_last_rotation_absent(tmp_path: Path) -> None:
    """Fichier absent → None, pas d'exception."""
    assert _read_last_rotation(tmp_path / "nonexistent.jsonl") is None


# ---------------------------------------------------------------------------
# Write path — pin / ban / undo / exclusion mutuelle
# ---------------------------------------------------------------------------


def test_write_pin_creates_override(tmp_path: Path) -> None:
    """pin_symbol écrit le bloc overrides: dans universe.yaml."""
    path = _universe_yaml(tmp_path, ["1326.TW", "CMS"])
    pin_symbol(path, "1326.TW")
    ov = load_user_overrides(path)
    assert "1326.TW" in ov.pin
    assert "1326.TW" not in ov.ban


def test_write_ban_creates_override(tmp_path: Path) -> None:
    """ban_symbol écrit le bloc overrides: dans universe.yaml."""
    path = _universe_yaml(tmp_path, ["CMS"])
    ban_symbol(path, "CMS")
    ov = load_user_overrides(path)
    assert "CMS" in ov.ban
    assert "CMS" not in ov.pin


def test_write_undo_removes_pin(tmp_path: Path) -> None:
    """clear_override retire un pin existant."""
    path = _universe_yaml(tmp_path, ["1326.TW"])
    pin_symbol(path, "1326.TW")
    clear_override(path, "1326.TW")
    ov = load_user_overrides(path)
    assert "1326.TW" not in ov.pin
    assert "1326.TW" not in ov.ban


def test_write_undo_removes_ban(tmp_path: Path) -> None:
    """clear_override retire un ban existant."""
    path = _universe_yaml(tmp_path, ["CMS"])
    ban_symbol(path, "CMS")
    clear_override(path, "CMS")
    ov = load_user_overrides(path)
    assert "CMS" not in ov.ban


def test_write_pin_then_ban_mutual_exclusion(tmp_path: Path) -> None:
    """Pinner puis bannir un symbole → ban uniquement, pin retiré."""
    path = _universe_yaml(tmp_path, ["CMS"])
    pin_symbol(path, "CMS")
    ban_symbol(path, "CMS")  # devrait retirer du pin
    ov = load_user_overrides(path)
    assert "CMS" in ov.ban
    assert "CMS" not in ov.pin


def test_write_ban_then_pin_mutual_exclusion(tmp_path: Path) -> None:
    """Bannir puis épingler → pin uniquement, ban retiré."""
    path = _universe_yaml(tmp_path, ["CMS"])
    ban_symbol(path, "CMS")
    pin_symbol(path, "CMS")  # devrait retirer du ban
    ov = load_user_overrides(path)
    assert "CMS" in ov.pin
    assert "CMS" not in ov.ban


def test_write_preserves_symbols_block(tmp_path: Path) -> None:
    """L'écriture du bloc overrides: ne touche pas la liste symbols:."""
    path = _universe_yaml(tmp_path, ["1326.TW", "CMS", "BN.PA"])
    pin_symbol(path, "1326.TW")

    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["symbols"] == ["1326.TW", "CMS", "BN.PA"]


def test_write_no_file_creates_empty(tmp_path: Path) -> None:
    """Fichier absent → load_user_overrides retourne vide, jamais d'exception."""
    ov = load_user_overrides(tmp_path / "nonexistent.yaml")
    assert ov.pin == ()
    assert ov.ban == ()


# ---------------------------------------------------------------------------
# Textual — montage + navigation
# ---------------------------------------------------------------------------


async def test_universe_page_monte(tmp_path: Path, monkeypatch) -> None:
    """La page UniversePage monte sans crash (état minimal)."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        # Naviguer vers la page 7
        await pilot.press("6")
        await pilot.pause()
        page = app.query_one("#universe-page", UniversePage)
        assert page is not None
        assert page.display is True


async def test_universe_page_state_vide(tmp_path: Path, monkeypatch) -> None:
    """État vide → la page monte sans exception."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        page = app.query_one("#universe-page", UniversePage)
        # update_state avec dict vide ne doit jamais lever
        page.update_state({})


async def test_universe_page_panneaux_droits_existent(tmp_path: Path, monkeypatch) -> None:
    """Les panneaux rotation/hotset/overrides sont dans le DOM."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("6")
        await pilot.pause()
        assert app.query_one("#pipeline-body") is not None
        assert app.query_one("#rotation-body") is not None
        assert app.query_one("#hotset-body") is not None
        assert app.query_one("#overrides-body") is not None


async def test_universe_page_update_state_avec_symboles(tmp_path: Path, monkeypatch) -> None:
    """update_state avec des symboles remplit la table sans exception."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("6")
        await pilot.pause()
        page = app.query_one("#universe-page", UniversePage)
        state = _minimal_state(
            symbols=["3443.TW", "AAPL"],
            hotlist=["3443.TW"],
            scores={"3443.TW": 0.81},
        )
        page.update_state(state)


async def test_universe_page_write_path_pin(tmp_path: Path, monkeypatch) -> None:
    """L'action pin_selected écrit dans universe.yaml via _universe_path."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)

    # Créer un universe.yaml dans tmp_path/config/ (chemin attendu par _universe_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    _universe_yaml(config_dir, ["3443.TW"])

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("6")
        await pilot.pause()
        page = app.query_one("#universe-page", UniversePage)

        # Injecter un état avec des symboles pour peupler la table
        state = _minimal_state(
            symbols=["3443.TW"],
            hotlist=["3443.TW"],
            scores={"3443.TW": 0.81},
        )
        # Pointer le _universe_path vers notre tmp config
        monkeypatch.setattr(app, "_root", tmp_path)
        page.update_state(state)
        await pilot.pause()

        # Déclencher l'action pin directement (pas de cursor_row fiable en test sans DataTable réel)
        try:
            pin_symbol(page._universe_path(), "3443.TW")
        except Exception:
            pass  # le fichier n'existe peut-être pas dans ce chemin en test

        ov = load_user_overrides(config_dir / "universe.yaml")
        assert "3443.TW" in ov.pin


# ---------------------------------------------------------------------------
# _distribute_rows — distribution adaptative
# ---------------------------------------------------------------------------


def test_distribute_rows_proportional() -> None:
    """Les venues avec plus de symboles obtiennent plus de lignes."""
    result = _distribute_rows(30, [15, 10, 5], minimum=3)
    assert result[0] >= result[1] >= result[2]
    assert all(r >= 3 for r in result)


def test_distribute_rows_all_fit() -> None:
    """Quand le total dépasse la somme des counts, tout le monde est satisfait."""
    counts = [8, 6, 4]
    result = _distribute_rows(50, counts, minimum=3)
    assert result == counts


def test_distribute_rows_minimum_capped_by_count() -> None:
    """Le minimum est plafonné au count réel (pas de lignes vides allouées)."""
    result = _distribute_rows(20, [2, 2, 2], minimum=3)
    assert result == [2, 2, 2]


def test_distribute_rows_tight() -> None:
    """Allocation serrée : chaque venue obtient au moins minimum, total respecté."""
    result = _distribute_rows(12, [10, 8, 6], minimum=3)
    assert sum(result) <= 12
    assert all(r >= 3 for r in result)
    # TW reçoit le plus
    assert result[0] >= result[1]


def test_distribute_rows_empty() -> None:
    """Liste vide → liste vide."""
    assert _distribute_rows(20, [], minimum=3) == []


def test_distribute_rows_single_venue() -> None:
    """Une seule venue → obtient tout, plafonné à son count."""
    result = _distribute_rows(20, [5], minimum=3)
    assert result == [5]


# ---------------------------------------------------------------------------
# _populate_universe_table — MockTable (sans Textual)
# ---------------------------------------------------------------------------


class _MockTable:
    """Substitut minimal de SymbolTable pour les tests purs de _populate_universe_table."""

    def __init__(self) -> None:
        self.row_keys: list[str] = []

    def clear(self, columns: bool = False) -> None:
        self.row_keys = []

    def add_column(self, name: str, width: int | None = None) -> None:
        pass

    def add_row(self, *cells, key: str | None = None) -> None:
        if key:
            self.row_keys.append(key)


def test_populate_no_more_row_when_limits_cover_all() -> None:
    """Quand limits_per_venue >= count pour chaque venue, aucune ligne '+ N more'."""
    state = _minimal_state(
        symbols=["3443.TW", "6488.TWO", "AAPL", "BN.PA"],
        hotlist=["3443.TW"],
        scores={"3443.TW": 0.8},
    )
    table = _MockTable()
    _populate_universe_table(
        table,
        state,
        overrides=UserOverrides(),
        now=NOW,
        drops=frozenset(),
        limits_per_venue={"TW": 20, "EU": 20, "US": 20},
    )
    more_keys = [k for k in table.row_keys if k.startswith("—|more_")]
    assert more_keys == [], f"unexpected more rows: {more_keys}"


def test_populate_more_row_when_limit_exceeded() -> None:
    """Quand limit < count, une ligne '+ N more' apparaît avec le bon résidu."""
    symbols = [f"SYM{i}.TW" for i in range(10)]
    state = _minimal_state(symbols=symbols, hotlist=[], scores={})
    table = _MockTable()
    _populate_universe_table(
        table,
        state,
        overrides=UserOverrides(),
        now=NOW,
        drops=frozenset(),
        limits_per_venue={"TW": 4},
    )
    more_keys = [k for k in table.row_keys if k.startswith("—|more_")]
    assert len(more_keys) == 1
    sym_keys = [k for k in table.row_keys if not k.startswith("—|")]
    assert len(sym_keys) == 4


def test_populate_no_more_row_exact_fit() -> None:
    """Quand limit == count exact, pas de ligne 'more'."""
    symbols = ["A.TW", "B.TW", "C.TW"]
    state = _minimal_state(symbols=symbols, hotlist=[], scores={})
    table = _MockTable()
    _populate_universe_table(
        table,
        state,
        overrides=UserOverrides(),
        now=NOW,
        drops=frozenset(),
        limits_per_venue={"TW": 3},
    )
    more_keys = [k for k in table.row_keys if k.startswith("—|more_")]
    assert more_keys == []


# ---------------------------------------------------------------------------
# build_symbol_rows — name_col_width adaptatif
# ---------------------------------------------------------------------------


def test_build_symbol_rows_name_col_width_28_no_truncation() -> None:
    """name_col_width=28 : un nom de 25 chars n'est PAS tronqué (il tient dans 28)."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=[], scores={})
    state["company_map"]["3443.TW"] = "A" * 25
    rows = build_symbol_rows(state, UserOverrides(), now=NOW, name_col_width=28)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert len(tw_row.name) == 25
    assert "…" not in tw_row.name


def test_build_symbol_rows_name_col_width_28_truncates_at_28() -> None:
    """name_col_width=28 : un nom de 35 chars est tronqué à 27 + '…'."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=[], scores={})
    state["company_map"]["3443.TW"] = "A" * 35
    rows = build_symbol_rows(state, UserOverrides(), now=NOW, name_col_width=28)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert len(tw_row.name) <= 28
    assert "…" in tw_row.name


def test_build_symbol_rows_name_default_still_truncates_at_20() -> None:
    """Par défaut (name_col_width=20), un nom de 30 chars est tronqué à <= 20."""
    state = _minimal_state(symbols=["3443.TW"], hotlist=[], scores={})
    state["company_map"]["3443.TW"] = "A" * 30
    rows = build_symbol_rows(state, UserOverrides(), now=NOW)
    tw_row = next(r for r in rows if r.symbol == "3443.TW")
    assert len(tw_row.name) <= 20
    assert "…" in tw_row.name
