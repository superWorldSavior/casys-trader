"""Tests TDD pour trader/rotation_state.py — état persistant de la rotation."""
import json
import os
import tempfile

import pytest

from trader.rotation_state import load_rotation_state, save_rotation_state, seed_state, advance_state


_DEFAULT_STATE = {
    "current_hot_set": [],
    "dwell_days_by_symbol": {},
    "last_valid_universe": [],
    "last_rotation_at": "",
}


# ---------------------------------------------------------------------------
# load_rotation_state
# ---------------------------------------------------------------------------


class TestLoadRotationState:
    def test_fichier_absent_retourne_defaut(self, tmp_path):
        """Répertoire sans fichier → défaut vide, pas d'exception."""
        result = load_rotation_state(str(tmp_path))
        assert result == _DEFAULT_STATE

    def test_round_trip_avec_save(self, tmp_path):
        """save puis load → état identique."""
        state = {
            "current_hot_set": ["AAPL", "MSFT"],
            "dwell_days_by_symbol": {"AAPL": 3, "MSFT": 7},
            "last_valid_universe": ["AAPL", "MSFT", "GOOG"],
        }
        save_rotation_state(str(tmp_path), state)
        result = load_rotation_state(str(tmp_path))
        assert result == state

    def test_fichier_illisible_retourne_defaut(self, tmp_path):
        """Fichier corrompu → défaut, pas d'exception."""
        path = tmp_path / "rotation_state.json"
        path.write_text("{ invalid json }")
        result = load_rotation_state(str(tmp_path))
        assert result == _DEFAULT_STATE


# ---------------------------------------------------------------------------
# seed_state
# ---------------------------------------------------------------------------


class TestSeedState:
    def test_dwell_1_pour_chaque_symbole(self):
        """Bootstrap → dwell=1 pour chaque symbole du seed."""
        universe = ["AAPL", "MSFT", "GOOG"]
        result = seed_state(universe)
        assert result["dwell_days_by_symbol"] == {"AAPL": 1, "MSFT": 1, "GOOG": 1}

    def test_current_hot_set_egal_universe(self):
        """Bootstrap → current_hot_set == universe."""
        universe = ["AAPL", "MSFT"]
        result = seed_state(universe)
        assert result["current_hot_set"] == list(universe)

    def test_last_valid_universe_egal_universe(self):
        """Bootstrap → last_valid_universe == universe."""
        universe = ["AAPL", "MSFT"]
        result = seed_state(universe)
        assert result["last_valid_universe"] == list(universe)

    def test_seed_vide(self):
        """Bootstrap avec liste vide → structures vides cohérentes."""
        result = seed_state([])
        assert result == {
            "current_hot_set": [],
            "dwell_days_by_symbol": {},
            "last_valid_universe": [],
        }


# ---------------------------------------------------------------------------
# advance_state
# ---------------------------------------------------------------------------


class TestAdvanceState:
    def _base_state(self):
        return {
            "current_hot_set": ["AAPL", "MSFT"],
            "dwell_days_by_symbol": {"AAPL": 2, "MSFT": 4},
            "last_valid_universe": ["AAPL", "MSFT"],
        }

    def test_reste_chaud_dwell_incremente(self):
        """Symbole présent avant ET après → dwell += 1."""
        state = self._base_state()
        result = advance_state(state, ["AAPL", "MSFT"])
        assert result["dwell_days_by_symbol"]["AAPL"] == 3
        assert result["dwell_days_by_symbol"]["MSFT"] == 5

    def test_entrant_dwell_1(self):
        """Symbole nouveau dans new_hot_set → dwell = 1."""
        state = self._base_state()
        result = advance_state(state, ["AAPL", "GOOG"])
        assert result["dwell_days_by_symbol"]["GOOG"] == 1

    def test_sortant_absent_du_dwell(self):
        """Symbole retiré de new_hot_set → absent de dwell_days_by_symbol."""
        state = self._base_state()
        result = advance_state(state, ["AAPL"])
        assert "MSFT" not in result["dwell_days_by_symbol"]

    def test_current_hot_set_mis_a_jour(self):
        """current_hot_set reflète new_hot_set."""
        state = self._base_state()
        new = ["GOOG", "TSLA"]
        result = advance_state(state, new)
        assert result["current_hot_set"] == new

    def test_new_hot_set_vide_current_vide_last_conserve(self):
        """new_hot_set vide → current_hot_set vide, last_valid_universe inchangé."""
        state = self._base_state()
        result = advance_state(state, [])
        assert result["current_hot_set"] == []
        assert result["last_valid_universe"] == ["AAPL", "MSFT"]

    def test_new_hot_set_non_vide_last_mis_a_jour(self):
        """new_hot_set non vide → last_valid_universe = new_hot_set."""
        state = self._base_state()
        result = advance_state(state, ["GOOG"])
        assert result["last_valid_universe"] == ["GOOG"]

    def test_immutabilite_state_original(self):
        """advance_state ne modifie pas l'état d'entrée (retourne un nouvel objet)."""
        state = self._base_state()
        original_dwell = dict(state["dwell_days_by_symbol"])
        advance_state(state, ["AAPL"])
        assert state["dwell_days_by_symbol"] == original_dwell

    def test_last_valid_explicit_via_kwarg(self):
        """last_valid kwarg fourni → last_valid_universe = last_valid, pas new_hot_set."""
        state = self._base_state()
        hot_ns = ["AAPL"]         # hot non-sticky
        final = ["AAPL", "STICKY"]  # final = hot + sticky

        result = advance_state(state, hot_ns, last_valid=final)
        assert result["current_hot_set"] == hot_ns, "current_hot_set = hot non-sticky"
        assert result["last_valid_universe"] == final, (
            "last_valid_universe doit être last_valid kwarg, pas new_hot_set"
        )
        # STICKY ne doit PAS être dans current_hot_set ni dwell
        assert "STICKY" not in result["current_hot_set"]
        assert "STICKY" not in result["dwell_days_by_symbol"]

    def test_last_valid_none_falls_back_to_new_hot_set(self):
        """last_valid=None (défaut) → last_valid_universe = new_hot_set (comportement actuel)."""
        state = self._base_state()
        new = ["GOOG", "TSLA"]
        result = advance_state(state, new, last_valid=None)
        assert result["last_valid_universe"] == new

    def test_last_valid_vide_new_hot_set_non_vide(self):
        """last_valid=[] avec new_hot_set non vide → last_valid_universe = [] (kwarg prioritaire)."""
        state = self._base_state()
        result = advance_state(state, ["AAPL"], last_valid=[])
        assert result["last_valid_universe"] == []


# ---------------------------------------------------------------------------
# last_rotation_at — clé de gate temporelle
# ---------------------------------------------------------------------------


class TestLastRotationAt:
    def test_defaut_contient_last_rotation_at_vide(self, tmp_path):
        """Fichier absent → défaut inclut last_rotation_at=""."""
        result = load_rotation_state(str(tmp_path))
        assert "last_rotation_at" in result
        assert result["last_rotation_at"] == ""

    def test_advance_state_enregistre_rotation_at(self):
        """advance_state(..., rotation_at=...) met à jour last_rotation_at."""
        state = {
            "current_hot_set": ["AAPL"],
            "dwell_days_by_symbol": {"AAPL": 1},
            "last_valid_universe": ["AAPL"],
            "last_rotation_at": "",
        }
        result = advance_state(state, ["AAPL"], rotation_at="2026-06-15T20:00:00+00:00")
        assert result["last_rotation_at"] == "2026-06-15T20:00:00+00:00"

    def test_advance_state_sans_rotation_at_conserve_existant(self):
        """advance_state sans rotation_at → last_rotation_at inchangé."""
        state = {
            "current_hot_set": ["AAPL"],
            "dwell_days_by_symbol": {"AAPL": 1},
            "last_valid_universe": ["AAPL"],
            "last_rotation_at": "2026-06-15T15:30:00+00:00",
        }
        result = advance_state(state, ["AAPL"])
        assert result["last_rotation_at"] == "2026-06-15T15:30:00+00:00"

    def test_load_conserve_last_rotation_at_depuis_fichier(self, tmp_path):
        """save puis load → last_rotation_at conservé."""
        state = {
            "current_hot_set": ["AAPL"],
            "dwell_days_by_symbol": {"AAPL": 2},
            "last_valid_universe": ["AAPL"],
            "last_rotation_at": "2026-06-15T20:00:00+00:00",
        }
        save_rotation_state(str(tmp_path), state)
        result = load_rotation_state(str(tmp_path))
        assert result["last_rotation_at"] == "2026-06-15T20:00:00+00:00"


# ---------------------------------------------------------------------------
# save_rotation_state
# ---------------------------------------------------------------------------


class TestSaveRotationState:
    def test_cree_dossier_si_absent(self, tmp_path):
        """save crée le répertoire s'il n'existe pas."""
        target = str(tmp_path / "new_dir" / "sub")
        state = _DEFAULT_STATE.copy()
        save_rotation_state(target, state)
        assert os.path.exists(os.path.join(target, "rotation_state.json"))

    def test_json_indenté(self, tmp_path):
        """Le fichier produit est du JSON indenté (lisible)."""
        state = {
            "current_hot_set": ["AAPL"],
            "dwell_days_by_symbol": {"AAPL": 1},
            "last_valid_universe": ["AAPL"],
        }
        save_rotation_state(str(tmp_path), state)
        raw = (tmp_path / "rotation_state.json").read_text()
        # JSON indenté contient des newlines
        assert "\n" in raw
        # Parseable
        parsed = json.loads(raw)
        assert parsed == state

    def test_ecriture_atomique(self, tmp_path):
        """Pas de fichier tmp résiduel après save."""
        state = _DEFAULT_STATE.copy()
        save_rotation_state(str(tmp_path), state)
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "rotation_state.json"
