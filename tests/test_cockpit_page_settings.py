"""Tests de la page Settings (pages/settings.py).

Couvre :
- Builders purs (runtime, budget, rotation, data, risk, pending banner, intro)
- Loaders yaml sur des fichiers temporaires
- write_yaml_atomic : écriture, atomicité, plusieurs clés
- coerce_value : int / float / bool / str / erreurs
- risk.yaml jamais écrit (garde défensive)
- pending / revert (test de la mécanique via les builders)
- Montage Textual : panneaux présents dans le DOM + navigation 8
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from rich.console import Console

import trader.interfaces.cockpit.app as cockpit_module
import trader.interfaces.cockpit.pages.settings as settings_module
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.pages.settings import (
    EDITABLE_ROWS,
    SettingsPage,
    build_budget_panel,
    build_data_panel,
    build_intro,
    build_pending_banner,
    build_risk_panel,
    build_rotation_panel,
    build_runtime_panel,
    coerce_value,
    load_data_settings,
    load_env_display,
    load_portfolio_settings,
    load_radar_settings,
    load_risk_settings,
    write_yaml_atomic,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(renderable, *, width: int = 120) -> str:
    console = Console(width=width, legacy_windows=False, highlight=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _make_config_dir(tmp_path: Path) -> Path:
    """Crée un répertoire config minimal avec les 4 yaml requis."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "portfolio.yaml").write_text("starting_cash: 100000\n", encoding="utf-8")
    (config / "radar.yaml").write_text(
        "cap_m: 25\ndelta: 0.05\ndwell_days: 3\n"
        "override_enabled: true\npreopen_window_minutes: 90\n",
        encoding="utf-8",
    )
    (config / "risk.yaml").write_text(
        "max_gross_exposure: 100000\nmax_position_value: 30000\n"
        "max_order_value: 10000\nmax_risk_per_trade_pct: 0.01\n"
        "min_equity: 50000\nconfidence_gate_enabled: false\n"
        "require_hard_stop: false\n",
        encoding="utf-8",
    )
    (config / "data_sources.yaml").write_text(
        "profile: paper\nprofiles:\n  paper:\n    routes:\n"
        '      - symbols: ["*"]\n        sources: [yfinance, ib]\n',
        encoding="utf-8",
    )
    return config


def _make_minimal_state(tmp_path: Path) -> None:
    (tmp_path / "current_report.json").write_text(
        json.dumps(
            {
                "ts": "2026-07-06T10:00:00+00:00",
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


def _patch_app(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")


# ---------------------------------------------------------------------------
# Loaders purs
# ---------------------------------------------------------------------------


def test_load_portfolio_settings_lit_starting_cash(tmp_path):
    config = _make_config_dir(tmp_path)
    data = load_portfolio_settings(config)
    assert data["starting_cash"] == 100000


def test_load_portfolio_settings_fichier_absent(tmp_path):
    data = load_portfolio_settings(tmp_path / "nonexistent_config")
    assert data == {}


def test_load_radar_settings_lit_cap_m(tmp_path):
    config = _make_config_dir(tmp_path)
    data = load_radar_settings(config)
    assert data["cap_m"] == 25
    assert data["delta"] == pytest.approx(0.05)
    assert data["dwell_days"] == 3
    assert data["override_enabled"] is True
    assert data["preopen_window_minutes"] == 90


def test_load_risk_settings_lit_fusibles(tmp_path):
    config = _make_config_dir(tmp_path)
    data = load_risk_settings(config)
    assert data["max_gross_exposure"] == 100000
    assert data["confidence_gate_enabled"] is False


def test_load_data_settings_lit_profile(tmp_path):
    config = _make_config_dir(tmp_path)
    data = load_data_settings(config)
    assert data["profile"] == "paper"


def test_load_env_display_masque_les_cles_secretes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CASYS_IB_HOST=127.0.0.1\nOPENAI_API_KEY=sk-test\nCASYS_IB_PORT=4002\n",
        encoding="utf-8",
    )
    data = load_env_display(tmp_path)
    assert data["CASYS_IB_HOST"] == "127.0.0.1"
    assert data["OPENAI_API_KEY"] == "***"
    assert data["CASYS_IB_PORT"] == "4002"


def test_load_env_display_fichier_absent(tmp_path):
    data = load_env_display(tmp_path / "nowhere")
    assert data == {}


# ---------------------------------------------------------------------------
# coerce_value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,vtype,expected",
    [
        ("42", "int", 42),
        ("3.14", "float", 3.14),
        ("true", "bool", True),
        ("false", "bool", False),
        ("1", "bool", True),
        ("0", "bool", False),
        ("yes", "bool", True),
        ("no", "bool", False),
        ("hello", "str", "hello"),
    ],
)
def test_coerce_value_ok(raw, vtype, expected):
    value, err = coerce_value(raw, vtype)
    assert err is None
    if vtype == "float":
        assert value == pytest.approx(expected)
    else:
        assert value == expected


@pytest.mark.parametrize(
    "raw,vtype",
    [
        ("abc", "int"),
        ("abc", "float"),
        ("maybe", "bool"),
    ],
)
def test_coerce_value_erreur(raw, vtype):
    value, err = coerce_value(raw, vtype)
    assert value is None
    assert err is not None


# ---------------------------------------------------------------------------
# write_yaml_atomic
# ---------------------------------------------------------------------------


def test_write_yaml_atomic_portfolio(tmp_path):
    config = _make_config_dir(tmp_path)
    write_yaml_atomic(config, "portfolio", {"starting_cash": 200000})
    data = yaml.safe_load((config / "portfolio.yaml").read_text(encoding="utf-8"))
    assert data["starting_cash"] == 200000


def test_write_yaml_atomic_radar(tmp_path):
    config = _make_config_dir(tmp_path)
    write_yaml_atomic(config, "radar", {"cap_m": 10, "dwell_days": 5})
    data = yaml.safe_load((config / "radar.yaml").read_text(encoding="utf-8"))
    assert data["cap_m"] == 10
    assert data["dwell_days"] == 5
    # Clés non modifiées sont conservées
    assert "delta" in data


def test_write_yaml_atomic_preserve_clés_existantes(tmp_path):
    config = _make_config_dir(tmp_path)
    write_yaml_atomic(config, "radar", {"cap_m": 30})
    data = yaml.safe_load((config / "radar.yaml").read_text(encoding="utf-8"))
    # Les clés non touchées restent
    assert data["delta"] == pytest.approx(0.05)
    assert data["override_enabled"] is True


def test_write_yaml_atomic_risque_interdit(tmp_path):
    """risk.yaml ne doit JAMAIS être écrit par write_yaml_atomic."""
    config = _make_config_dir(tmp_path)
    with pytest.raises(ValueError, match="risk.yaml is read-only"):
        write_yaml_atomic(config, "risk", {"max_gross_exposure": 999999})
    # Le fichier ne doit pas avoir été modifié
    data = yaml.safe_load((config / "risk.yaml").read_text(encoding="utf-8"))
    assert data["max_gross_exposure"] == 100000


def test_write_yaml_atomic_no_leftover_tmp(tmp_path):
    """Aucun fichier .tmp ne doit rester après une écriture réussie."""
    config = _make_config_dir(tmp_path)
    write_yaml_atomic(config, "portfolio", {"starting_cash": 150000})
    tmp_files = list(config.glob("*.tmp"))
    assert tmp_files == []


def test_write_yaml_atomic_ajoute_header(tmp_path):
    """Les fichiers réécris contiennent un en-tête statique."""
    config = _make_config_dir(tmp_path)
    write_yaml_atomic(config, "portfolio", {"starting_cash": 150000})
    content = (config / "portfolio.yaml").read_text(encoding="utf-8")
    assert content.startswith("#")


# ---------------------------------------------------------------------------
# Builders purs
# ---------------------------------------------------------------------------


def test_build_intro_contient_config_yaml():
    rendered = _render(build_intro())
    assert "config/*.yaml" in rendered
    assert "daemon" in rendered
    assert "mandate" in rendered


def test_build_runtime_panel_affiche_ib():
    env = {"CASYS_IB_HOST": "192.168.1.1", "CASYS_IB_PORT": "4002", "CASYS_IB_CLIENT_ID": "17"}
    rendered = _render(build_runtime_panel(env))
    assert "192.168.1.1" in rendered
    assert "17" in rendered
    assert "4002" in rendered
    assert "restart required" in rendered


def test_build_runtime_panel_env_vide():
    """État vide : les tirets doivent apparaître, pas de crash."""
    rendered = _render(build_runtime_panel({}))
    assert "—" in rendered
    assert "restart required" in rendered


def test_build_budget_panel_affiche_starting_cash():
    portfolio = {"starting_cash": 100000}
    rendered = _render(build_budget_panel(portfolio, {}, cursor_key=None))
    assert "100000" in rendered
    assert "next cycle" in rendered


def test_build_budget_panel_cursor_affiche_enter_save():
    portfolio = {"starting_cash": 100000}
    rendered = _render(build_budget_panel(portfolio, {}, cursor_key="starting_cash"))
    assert "enter save" in rendered


def test_build_budget_panel_pending_affiche_was():
    portfolio = {"starting_cash": 100000}
    pending = {"starting_cash": 200000}
    rendered = _render(build_budget_panel(portfolio, pending, cursor_key=None))
    assert "200000" in rendered
    assert "was 100000" in rendered
    assert "● pending" in rendered


def test_build_budget_panel_etat_vide():
    rendered = _render(build_budget_panel({}, {}, cursor_key=None))
    assert "—" in rendered


def test_build_rotation_panel_affiche_toutes_les_cles():
    radar = {
        "cap_m": 25,
        "delta": 0.05,
        "dwell_days": 3,
        "override_enabled": True,
        "preopen_window_minutes": 90,
    }
    rendered = _render(build_rotation_panel(radar, {}, cursor_key=None))
    assert "25" in rendered
    assert "0.05" in rendered
    assert "3" in rendered
    assert "True" in rendered
    assert "90" in rendered
    assert "next rotation" in rendered


def test_build_rotation_panel_cursor_sur_cap_m():
    radar = {"cap_m": 25}
    rendered = _render(build_rotation_panel(radar, {}, cursor_key="cap_m"))
    assert "enter save" in rendered


def test_build_rotation_panel_pending():
    radar = {"delta": 0.05}
    pending = {"delta": 0.08}
    rendered = _render(build_rotation_panel(radar, pending, cursor_key=None))
    assert "0.08" in rendered
    assert "was 0.05" in rendered
    assert "● pending" in rendered


def test_build_rotation_panel_etat_vide():
    rendered = _render(build_rotation_panel({}, {}, cursor_key=None))
    assert "—" in rendered


def test_build_data_panel_affiche_profile():
    data = {
        "profile": "paper",
        "profiles": {
            "paper": {
                "routes": [{"symbols": ["*"], "sources": ["yfinance", "ib"]}]
            }
        },
    }
    rendered = _render(build_data_panel(data))
    assert "paper" in rendered
    assert "restart required" in rendered
    assert "yfinance" in rendered


def test_build_data_panel_etat_vide():
    rendered = _render(build_data_panel({}))
    assert "restart required" in rendered


def test_build_risk_panel_affiche_les_caps():
    risk = {
        "max_gross_exposure": 100000,
        "max_position_value": 30000,
        "max_order_value": 10000,
        "max_risk_per_trade_pct": 0.01,
        "min_equity": 50000,
        "confidence_gate_enabled": False,
        "require_hard_stop": False,
    }
    rendered = _render(build_risk_panel(risk))
    assert "$100,000" in rendered
    assert "$30,000" in rendered
    assert "1.0% of equity" in rendered
    assert "$50,000" in rendered
    assert "non-negotiable" in rendered
    assert "never write it" in rendered


def test_build_risk_panel_etat_vide():
    """État vide : tirets, pas de crash."""
    rendered = _render(build_risk_panel({}))
    assert "—" in rendered
    assert "non-negotiable" in rendered


def test_build_pending_banner_none_si_vide():
    assert build_pending_banner({}) is None


def test_build_pending_banner_affiche_n_et_cles():
    pending = {"cap_m": 10, "delta": 0.08}
    rendered = _render(build_pending_banner(pending))
    assert "2 pending changes" in rendered
    assert "cap_m" in rendered
    assert "delta" in rendered
    assert "write to config/" in rendered
    assert "revert all" in rendered


def test_build_pending_banner_singulier():
    pending = {"cap_m": 10}
    rendered = _render(build_pending_banner(pending))
    assert "1 pending change" in rendered


# ---------------------------------------------------------------------------
# EDITABLE_ROWS : registre correct
# ---------------------------------------------------------------------------


def test_editable_rows_contient_portfolio_et_radar():
    yaml_files = {r.yaml_file for r in EDITABLE_ROWS}
    assert "portfolio" in yaml_files
    assert "radar" in yaml_files


def test_editable_rows_risk_absent():
    """risk.yaml ne doit JAMAIS être dans le registre des lignes éditables."""
    assert all(r.yaml_file != "risk" for r in EDITABLE_ROWS)


def test_editable_rows_types_valides():
    valid_types = {"int", "float", "bool", "str"}
    for row in EDITABLE_ROWS:
        assert row.vtype in valid_types, f"{row.key}: type {row.vtype!r} inconnu"


def test_editable_rows_effects_valides():
    valid = {"next cycle", "next rotation", "restart required", "applies now", "locked"}
    for row in EDITABLE_ROWS:
        assert row.effect in valid, f"{row.key}: effect {row.effect!r} inconnu"


# ---------------------------------------------------------------------------
# Montage Textual
# ---------------------------------------------------------------------------


async def test_settings_page_monte_sans_crash(tmp_path, monkeypatch):
    """SettingsPage monte avec un répertoire config vide — pas de crash."""
    _make_minimal_state(tmp_path)
    _patch_app(monkeypatch, tmp_path)
    # Pointer _CONFIG_DIR vers un répertoire config vide (yaml absents → dict vides)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(settings_module, "_CONFIG_DIR", config_dir)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        # Naviguer vers la page settings (touche 8)
        await pilot.press("8")
        await pilot.pause()
        # Les panneaux principaux doivent être dans le DOM
        assert app.query_one("#runtime-panel") is not None
        assert app.query_one("#budget-panel") is not None
        assert app.query_one("#rotation-panel") is not None
        assert app.query_one("#risk-panel") is not None
        assert app.query_one("#data-panel") is not None


async def test_settings_page_monte_avec_yaml_reels(tmp_path, monkeypatch):
    """SettingsPage monte avec des yaml réels — les corps sont non-vides."""
    _make_minimal_state(tmp_path)
    _patch_app(monkeypatch, tmp_path)
    config_dir = _make_config_dir(tmp_path)
    monkeypatch.setattr(settings_module, "_CONFIG_DIR", config_dir)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("8")
        await pilot.pause()
        page = app.query_one("#settings-page", SettingsPage)
        assert page is not None
        # update_state ne doit pas lever d'exception
        page.update_state({})


async def test_settings_page_update_state_etat_vide(tmp_path, monkeypatch):
    """update_state({}) avec yaml absents ne lève pas d'exception."""
    _make_minimal_state(tmp_path)
    _patch_app(monkeypatch, tmp_path)
    empty_config = tmp_path / "empty_config"
    empty_config.mkdir()
    monkeypatch.setattr(settings_module, "_CONFIG_DIR", empty_config)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        page = app.query_one("#settings-page", SettingsPage)
        # Doit rester silencieux même avec yaml absents
        page.update_state({})
        page.update_state({"starting_cash": None})


async def test_settings_page_risk_panel_non_editable(tmp_path, monkeypatch):
    """Le panneau RISK GATE ne doit jamais être écrit, même après w."""
    _make_minimal_state(tmp_path)
    _patch_app(monkeypatch, tmp_path)
    config_dir = _make_config_dir(tmp_path)
    monkeypatch.setattr(settings_module, "_CONFIG_DIR", config_dir)

    original_risk = (config_dir / "risk.yaml").read_text(encoding="utf-8")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("8")
        await pilot.pause()
        page = app.query_one("#settings-page", SettingsPage)
        # Simuler un pending (sans passer par risk.yaml)
        page._pending["cap_m"] = 99
        # Écrire via action
        page.action_write_settings()
        await pilot.pause()

    # risk.yaml ne doit pas avoir été modifié
    assert (config_dir / "risk.yaml").read_text(encoding="utf-8") == original_risk


async def test_settings_page_pending_banner_visible_quand_pending(tmp_path, monkeypatch):
    """La bannière pending est visible dès qu'il y a des changements."""
    _make_minimal_state(tmp_path)
    _patch_app(monkeypatch, tmp_path)
    config_dir = _make_config_dir(tmp_path)
    monkeypatch.setattr(settings_module, "_CONFIG_DIR", config_dir)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("8")
        await pilot.pause()
        page = app.query_one("#settings-page", SettingsPage)
        from textual.widgets import Static
        banner = page.query_one("#pending-banner", Static)

        # Sans pending : bannière cachée
        assert banner.display is False

        # Avec pending : bannière visible
        page._pending["cap_m"] = 99
        page._render_all()
        assert banner.display is True

        # Après revert : bannière cachée à nouveau
        page.action_revert_settings()
        assert banner.display is False
