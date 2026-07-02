"""Tests for rotation_collectors — TDD strict."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_position(qty: float) -> SimpleNamespace:
    """Fake position avec un champ quantity."""
    return SimpleNamespace(quantity=qty)


def _make_plan(symbol: str) -> SimpleNamespace:
    """Fake TradePlan avec un champ .symbol."""
    return SimpleNamespace(symbol=symbol)


# ---------------------------------------------------------------------------
# sticky_collector
# ---------------------------------------------------------------------------


class TestStickyCollector:
    def test_union_of_positions_and_plans(self):
        """AAA (qty=10) + BBB (plan) → {'AAA','BBB'} ; FLAT (qty=0) exclu."""
        from trader.rotation.collectors import sticky_collector

        positions_fn = lambda: {  # noqa: E731
            "AAA": _make_position(10),
            "FLAT": _make_position(0),
        }
        plans_fn = lambda: [_make_plan("BBB")]  # noqa: E731

        result = sticky_collector(positions_fn=positions_fn, plans_fn=plans_fn)

        assert result == {"AAA", "BBB"}

    def test_zero_quantity_excluded(self):
        """Une position à qty=0 n'est pas sticky."""
        from trader.rotation.collectors import sticky_collector

        positions_fn = lambda: {"FLAT": _make_position(0.0)}  # noqa: E731
        plans_fn = lambda: []  # noqa: E731

        assert sticky_collector(positions_fn=positions_fn, plans_fn=plans_fn) == set()

    def test_empty_inputs_return_empty_set(self):
        from trader.rotation.collectors import sticky_collector

        assert sticky_collector(positions_fn=lambda: {}, plans_fn=lambda: []) == set()

    def test_negative_quantity_is_sticky(self):
        """Position short (qty < 0) est non-nulle → sticky."""
        from trader.rotation.collectors import sticky_collector

        positions_fn = lambda: {"SHORT": _make_position(-5)}  # noqa: E731
        assert sticky_collector(positions_fn=positions_fn, plans_fn=lambda: []) == {"SHORT"}

    def test_duplicate_symbol_in_both_sources(self):
        """Si un symbole est en position ET en plan, il apparaît une seule fois."""
        from trader.rotation.collectors import sticky_collector

        positions_fn = lambda: {"AAA": _make_position(3)}  # noqa: E731
        plans_fn = lambda: [_make_plan("AAA")]  # noqa: E731

        assert sticky_collector(positions_fn=positions_fn, plans_fn=plans_fn) == {"AAA"}


# ---------------------------------------------------------------------------
# build_positions_fn / build_plans_fn — fail-safe sur state_dir vide
# ---------------------------------------------------------------------------


class TestBuildPositionsFn:
    def test_empty_state_dir_returns_empty_dict(self, tmp_path: Path):
        """Aucun broker.json → closure retourne {} sans lever."""
        from trader.rotation.collectors import build_positions_fn

        fn = build_positions_fn(tmp_path)
        result = fn()
        assert result == {}

    def test_returns_callable(self, tmp_path: Path):
        from trader.rotation.collectors import build_positions_fn

        fn = build_positions_fn(tmp_path)
        assert callable(fn)


class TestBuildPlansFn:
    def test_empty_state_dir_returns_empty_list(self, tmp_path: Path):
        """Aucun plans.json → closure retourne [] sans lever."""
        from trader.rotation.collectors import build_plans_fn

        fn = build_plans_fn(tmp_path)
        result = fn()
        # Doit être un conteneur vide itérable
        assert list(result) == []

    def test_returns_callable(self, tmp_path: Path):
        from trader.rotation.collectors import build_plans_fn

        fn = build_plans_fn(tmp_path)
        assert callable(fn)

    def test_lit_le_fichier_ecrit_par_le_daemon(self, tmp_path: Path):
        """build_plans_fn doit lire le MÊME fichier que le daemon écrit
        (trade_plans.json, daemon.py). Sinon la branche plans du sticky est morte
        et un trade plan ne devient jamais sticky (régression D10)."""
        from trader.rotation.collectors import build_plans_fn
        from trader.planning.trade_plan import TradePlanStore, create_trade_plan

        # Le daemon écrit STATE_DIR / "trade_plans.json".
        store = TradePlanStore(tmp_path / "trade_plans.json")
        store.upsert(
            create_trade_plan(
                symbol="SPY",
                side="LONG",
                quantity=10.0,
                entry_price=100.0,
                opened_at="2026-06-05T12:00:00+00:00",
                raw_exit_plan={"hard_stop": 95.0},
            )
        )

        plans = build_plans_fn(tmp_path)()

        assert [p.symbol for p in plans] == ["SPY"]


# ---------------------------------------------------------------------------
# default_override_fn
# ---------------------------------------------------------------------------


class TestDefaultOverrideFn:
    def test_returns_noop_override(self):
        from trader.rotation.collectors import default_override_fn

        result = default_override_fn({"symbols": ["AAA", "BBB"]})
        assert result == {"add": [], "remove": []}

    def test_noop_regardless_of_payload(self):
        from trader.rotation.collectors import default_override_fn

        assert default_override_fn(None) == {"add": [], "remove": []}
        assert default_override_fn({}) == {"add": [], "remove": []}
        assert default_override_fn("anything") == {"add": [], "remove": []}
