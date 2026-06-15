"""Tests TDD pour trader/rotation.py — veille deux niveaux."""
import pytest
import os
import tempfile

import yaml

from trader.rotation import apply_hysteresis, apply_override, compose_final, emergency_exits, sticky_symbols, write_universe_atomic
from trader.rotation import UniverseWriteError


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


# ---------------------------------------------------------------------------
# compose_final
# ---------------------------------------------------------------------------

class TestComposeFinal:
    def test_sticky_over_cap_alert(self):
        """3 sticky + cap_m=2 → tous présents + alert='sticky_over_cap'."""
        final, alert = compose_final(
            default_hot=["X", "Y"],
            sticky={"A", "B", "C"},
            cap_m=2,
        )
        assert set(final) >= {"A", "B", "C"}
        assert alert == "sticky_over_cap"

    def test_slot_libre_depuis_default(self):
        """1 sticky + default [X,Y,Z] + cap_m=2 → {sticky, X}, alert=None."""
        final, alert = compose_final(
            default_hot=["X", "Y", "Z"],
            sticky={"S"},
            cap_m=2,
        )
        # 1 sticky + 1 slot libre → 1 élément de default_hot
        assert "S" in final
        assert len(final) == 2
        assert final[len(final) - 1] == "X"  # premier de default_hot non-sticky
        assert alert is None

    def test_pas_de_doublons(self):
        """Sticky qui est aussi dans default_hot → pas de doublon."""
        final, alert = compose_final(
            default_hot=["A", "B"],
            sticky={"A"},
            cap_m=2,
        )
        assert final.count("A") == 1
        assert alert is None


# ---------------------------------------------------------------------------
# apply_override
# ---------------------------------------------------------------------------

class TestApplyOverride:
    def test_add_remove_valides(self):
        """Add et remove valides → pas de rejets."""
        result, rejections = apply_override(
            default_hot=["A", "B", "C"],
            add=["D"],
            remove=["C"],
            pool={"A", "B", "C", "D"},
            sticky=set(),
            free_slots=3,
        )
        assert "D" in result
        assert "C" not in result
        assert rejections == []

    def test_out_of_pool_rejete(self):
        """Ajout d'un symbole absent du pool → rejet out_of_pool."""
        result, rejections = apply_override(
            default_hot=["A", "B"],
            add=["Z"],
            remove=[],
            pool={"A", "B"},
            sticky=set(),
            free_slots=3,
        )
        assert "Z" not in result
        assert {"symbol": "Z", "reason": "out_of_pool"} in rejections

    def test_remove_sticky_rejete(self):
        """Retrait d'un symbole sticky → rejet sticky_protected, non retiré."""
        result, rejections = apply_override(
            default_hot=["A", "B"],
            add=[],
            remove=["A"],
            pool={"A", "B"},
            sticky={"A"},
            free_slots=2,
        )
        assert "A" in result
        assert {"symbol": "A", "reason": "sticky_protected"} in rejections

    def test_add_depasse_free_slots_rejete(self):
        """Ajout qui dépasse free_slots → rejet cap_exceeded."""
        result, rejections = apply_override(
            default_hot=["A", "B"],
            add=["C", "D"],
            remove=[],
            pool={"A", "B", "C", "D"},
            sticky=set(),
            free_slots=2,
        )
        # A, B occupent déjà 2 slots ; C passerait à 3, D à 4 → les 2 rejetés
        assert "C" not in result
        assert "D" not in result
        reasons = [r["reason"] for r in rejections]
        assert "cap_exceeded" in reasons


# ---------------------------------------------------------------------------
# write_universe_atomic
# ---------------------------------------------------------------------------

class TestWriteUniverseAtomic:
    def test_ecrit_yaml_relisible(self, tmp_path):
        """Écrit un fichier YAML avec la clé 'symbols' et relisible via yaml.safe_load."""
        path = tmp_path / "universe.yaml"
        write_universe_atomic(str(path), ["AAPL", "MSFT", "NVDA"])
        content = yaml.safe_load(path.read_text())
        assert content == {"symbols": ["AAPL", "MSFT", "NVDA"]}

    def test_aucun_fichier_temporaire_residuel(self, tmp_path):
        """Après écriture, aucun fichier .tmp ne subsiste dans le dossier."""
        path = tmp_path / "universe.yaml"
        write_universe_atomic(str(path), ["AAPL"])
        residuals = list(tmp_path.glob("*.tmp"))
        assert residuals == []

    def test_symbols_vide_leve_universe_write_error(self, tmp_path):
        """symbols vide → lève UniverseWriteError."""
        path = tmp_path / "universe.yaml"
        with pytest.raises(UniverseWriteError):
            write_universe_atomic(str(path), [])
