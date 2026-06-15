"""Tests TDD pour trader/rotation.py — veille deux niveaux."""
import pytest
from trader.rotation import apply_hysteresis


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
