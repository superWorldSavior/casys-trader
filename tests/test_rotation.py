"""Tests TDD pour trader/rotation.py — veille deux niveaux."""
import pytest
from trader.rotation import apply_hysteresis, emergency_exits, sticky_symbols


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(symbol: str, attractiveness: float) -> dict:
    return {"symbol": symbol, "attractiveness": attractiveness}


# ---------------------------------------------------------------------------
# apply_hysteresis
# ---------------------------------------------------------------------------

class TestApplyHysteresis:
    def test_swap_faible_evictable(self):
        """C(2.0) > A(1.0) > B(0.9), dwell A=B=5 >= 3 → C et A (B éjecté)."""
        ranked = [
            _item("C", 2.0),
            _item("A", 1.0),
            _item("B", 0.9),
        ]
        result = apply_hysteresis(
            ranked,
            current={"A", "B"},
            dwell={"A": 5, "B": 5},
            cap_m=2,
            delta=0.05,
            dwell_days=3,
        )
        assert set(result) == {"C", "A"}

    def test_pas_de_swap_dwell_insuffisant(self):
        """Même setup mais dwell=1 < 3 → pas de swap, {A, B} conservés."""
        ranked = [
            _item("C", 2.0),
            _item("A", 1.0),
            _item("B", 0.9),
        ]
        result = apply_hysteresis(
            ranked,
            current={"A", "B"},
            dwell={"A": 1, "B": 1},
            cap_m=2,
            delta=0.05,
            dwell_days=3,
        )
        assert set(result) == {"A", "B"}

    def test_pas_de_swap_delta_insuffisant(self):
        """C attr=0.92, B attr=0.90, delta=0.05 → 0.92 < 0.90+0.05 → pas de swap."""
        ranked = [
            _item("C", 0.92),
            _item("A", 1.0),
            _item("B", 0.90),
        ]
        result = apply_hysteresis(
            ranked,
            current={"A", "B"},
            dwell={"A": 5, "B": 5},
            cap_m=2,
            delta=0.05,
            dwell_days=3,
        )
        assert set(result) == {"A", "B"}

    def test_slot_libre_rempli(self):
        """current={A}, cap_m=2, ranked A(1.0), C(0.8) → {A, C}."""
        ranked = [
            _item("A", 1.0),
            _item("C", 0.8),
        ]
        result = apply_hysteresis(
            ranked,
            current={"A"},
            dwell={"A": 0},
            cap_m=2,
            delta=0.05,
            dwell_days=3,
        )
        assert set(result) == {"A", "C"}


# ---------------------------------------------------------------------------
# emergency_exits
# ---------------------------------------------------------------------------

class TestEmergencyExits:
    def test_sous_le_floor_evince(self):
        """Symbole chaud avec attractiveness < emergency_floor → évincé."""
        ranked = [_item("A", 0.3), _item("B", 1.0)]
        result = emergency_exits(
            hot_set={"A", "B"},
            ranked=ranked,
            emergency_floor=0.5,
        )
        assert result == {"A"}

    def test_dans_gap_adverse_evince(self):
        """Symbole chaud dans gap_adverse → évincé."""
        ranked = [_item("A", 0.9), _item("B", 1.0)]
        result = emergency_exits(
            hot_set={"A", "B"},
            ranked=ranked,
            emergency_floor=0.1,
            gap_adverse=frozenset({"A"}),
        )
        assert result == {"A"}

    def test_dans_daily_invalidated_evince(self):
        """Symbole chaud dans daily_invalidated → évincé."""
        ranked = [_item("A", 0.9), _item("B", 1.0)]
        result = emergency_exits(
            hot_set={"A", "B"},
            ranked=ranked,
            emergency_floor=0.1,
            daily_invalidated=frozenset({"A"}),
        )
        assert result == {"A"}

    def test_aucun_evince_si_tout_ok(self):
        """Rien à évincer → set vide."""
        ranked = [_item("A", 0.9), _item("B", 1.0)]
        result = emergency_exits(
            hot_set={"A", "B"},
            ranked=ranked,
            emergency_floor=0.5,
        )
        assert result == set()


# ---------------------------------------------------------------------------
# sticky_symbols
# ---------------------------------------------------------------------------

class TestStickySymbols:
    def test_union_des_quatre_sources(self):
        """sticky_symbols retourne l'union correcte des 4 sets."""
        result = sticky_symbols(
            positions={"AAPL", "MSFT"},
            armed_plans={"TSLA"},
            exit_watches={"NVDA"},
            pending_orders={"AMD"},
        )
        assert result == {"AAPL", "MSFT", "TSLA", "NVDA", "AMD"}

    def test_union_vide_si_tout_vide(self):
        result = sticky_symbols(
            positions=set(),
            armed_plans=set(),
            exit_watches=set(),
            pending_orders=set(),
        )
        assert result == set()
