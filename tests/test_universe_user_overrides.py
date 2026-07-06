"""Invariants du domaine user_overrides (pin/ban cockpit sur universe.yaml).

Le bloc ``overrides:`` a deux propriétaires : la rotation possède ``symbols:``,
le cockpit possède ``overrides:`` — chacun préserve le bloc de l'autre.
"""

from __future__ import annotations

import yaml

from trader.market.rotation.core import write_universe_atomic
from trader.market.rotation.user_overrides import (
    UserOverrides,
    apply_user_overrides,
    ban_symbol,
    clear_override,
    load_user_overrides,
    pin_symbol,
    save_user_overrides,
)
from trader.market.rotation.venues import tick


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty_overrides(tmp_path):
    assert load_user_overrides(tmp_path / "universe.yaml") == UserOverrides()


def test_load_corrupt_file_returns_empty_overrides(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("{ not yaml :::", encoding="utf-8")
    assert load_user_overrides(path) == UserOverrides()


def test_load_file_without_overrides_block(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("symbols:\n- AAPL\n", encoding="utf-8")
    assert load_user_overrides(path) == UserOverrides()


def test_save_preserves_symbols_block(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("symbols:\n- AAPL\n- 2330.TW\n", encoding="utf-8")

    save_user_overrides(path, UserOverrides(pin=("PANW",), ban=("3443.TW",)))

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["symbols"] == ["AAPL", "2330.TW"]
    assert data["overrides"] == {"pin": ["PANW"], "ban": ["3443.TW"]}


def test_save_empty_overrides_omits_block(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("symbols:\n- AAPL\noverrides:\n  pin:\n  - PANW\n", encoding="utf-8")

    save_user_overrides(path, UserOverrides())

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "overrides" not in data
    assert data["symbols"] == ["AAPL"]


def test_load_pin_wins_over_ban_on_conflicting_file(tmp_path):
    """Fichier édité à la main avec un symbole dans les deux → pin gagne."""
    path = tmp_path / "universe.yaml"
    path.write_text(
        "symbols: [AAPL]\noverrides:\n  pin: [AAPL]\n  ban: [AAPL]\n", encoding="utf-8"
    )
    overrides = load_user_overrides(path)
    assert overrides.pin == ("AAPL",)
    assert overrides.ban == ()


# ---------------------------------------------------------------------------
# pin / ban / clear — exclusion mutuelle
# ---------------------------------------------------------------------------


def test_pin_then_ban_moves_symbol(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("symbols: [AAPL]\n", encoding="utf-8")

    assert pin_symbol(path, "AAPL").pin == ("AAPL",)
    after_ban = ban_symbol(path, "AAPL")
    assert after_ban.pin == ()
    assert after_ban.ban == ("AAPL",)


def test_clear_removes_from_both(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("symbols: [AAPL]\n", encoding="utf-8")
    pin_symbol(path, "AAPL")
    ban_symbol(path, "2330.TW")

    clear_override(path, "AAPL")
    clear_override(path, "2330.TW")

    assert load_user_overrides(path) == UserOverrides()
    assert "overrides" not in yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pin_is_idempotent(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("symbols: [AAPL]\n", encoding="utf-8")
    pin_symbol(path, "PANW")
    assert pin_symbol(path, "PANW").pin == ("PANW",)


# ---------------------------------------------------------------------------
# write_universe_atomic préserve le bloc overrides (anti-régression critique)
# ---------------------------------------------------------------------------


def test_rotation_write_preserves_overrides_block(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("symbols: [AAPL]\n", encoding="utf-8")
    pin_symbol(path, "PANW")
    ban_symbol(path, "3443.TW")

    write_universe_atomic(str(path), ["2330.TW", "AAPL"])

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["symbols"] == ["2330.TW", "AAPL"]
    assert data["overrides"] == {"pin": ["PANW"], "ban": ["3443.TW"]}


def test_rotation_write_without_overrides_stays_clean(tmp_path):
    path = tmp_path / "universe.yaml"
    write_universe_atomic(str(path), ["AAPL"])
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"symbols": ["AAPL"]}


# ---------------------------------------------------------------------------
# apply_user_overrides — sémantique pin/ban/sticky
# ---------------------------------------------------------------------------


def test_apply_ban_removes_from_selection():
    assert apply_user_overrides(["A", "B", "C"], ban=("B",)) == ["A", "C"]


def test_apply_ban_never_ejects_sticky():
    """Position ouverte bannie → reste gérée (quitte seulement la sélection)."""
    result = apply_user_overrides(["A", "B"], ban=("A",), sticky={"A"})
    assert result == ["A", "B"]


def test_apply_pin_appends_missing_symbol():
    assert apply_user_overrides(["A"], pin=("Z",)) == ["A", "Z"]


def test_apply_pin_does_not_duplicate():
    assert apply_user_overrides(["A", "Z"], pin=("Z",)) == ["A", "Z"]


# ---------------------------------------------------------------------------
# Intégration tick : overrides appliqués à l'univers écrit
# ---------------------------------------------------------------------------


def _write_tick_config(config_dir):
    (config_dir / "config").mkdir()
    (config_dir / "config" / "sessions.yaml").write_text(
        'TW: {open: "01:00", close: "05:30"}\n'
        'EU: {open: "07:00", close: "15:30"}\n'
        'US: {open: "13:30", close: "20:00"}\n',
        encoding="utf-8",
    )
    (config_dir / "radar.yaml").write_text(
        "cap_m: 5\ndelta: 0.05\ndwell_days: 1\nemergency_score: -1\n",
        encoding="utf-8",
    )


def _rank_obj():
    def _item(symbol, attractiveness):
        return {
            "symbol": symbol,
            "attractiveness": attractiveness,
            "bias": "long",
            "directional_score": attractiveness,
        }

    return {
        "ranked": [_item("8299.TWO", 2.0), _item("2330.TW", 1.9)],
        "gap_adverse": frozenset(),
        "ineligible": {},
        "components_by_symbol": {},
    }


def test_tick_applies_ban_and_pin_to_written_universe(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    _write_tick_config(config_dir)
    universe_path = config_dir / "universe.yaml"
    universe_path.write_text("symbols: [AAPL]\n", encoding="utf-8")
    ban_symbol(universe_path, "2330.TW")
    pin_symbol(universe_path, "PANW")

    result = tick(
        config_dir,
        state_dir,
        "2026-06-15T03:00:00+00:00",
        rank_fn=_rank_obj,
        sticky_fn=lambda: set(),
        fx_cap=3,
    )

    assert "2330.TW" not in result["final"]
    assert "PANW" in result["final"]
    data = yaml.safe_load(universe_path.read_text(encoding="utf-8"))
    assert data["symbols"] == result["final"]
    # le write de la rotation n'a pas détruit le bloc du cockpit
    assert data["overrides"] == {"pin": ["PANW"], "ban": ["2330.TW"]}


def test_tick_ban_on_sticky_symbol_keeps_it_managed(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    _write_tick_config(config_dir)
    universe_path = config_dir / "universe.yaml"
    universe_path.write_text("symbols: [AAPL]\n", encoding="utf-8")
    ban_symbol(universe_path, "ZZZ.TW")

    result = tick(
        config_dir,
        state_dir,
        "2026-06-15T03:00:00+00:00",
        rank_fn=_rank_obj,
        sticky_fn=lambda: {"ZZZ.TW"},
        fx_cap=3,
    )

    assert "ZZZ.TW" in result["final"]
