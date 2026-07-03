"""Tests TDD pour write_json_atomic (Task 3 §1a).

Cas couverts :
- le JSON produit est relisible et identique à json.dumps(indent=2, ensure_ascii=False)
- l'écriture passe par un .tmp puis os.replace → jamais de fichier cible partiellement écrit
- un tmp résiduel ne corrompt pas la cible existante
"""

import json
import os
from pathlib import Path

import pytest

from trader.state_db.shadow import write_json_atomic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DATA = {
    "cash": 100_000.0,
    "positions": {"AAPL": {"quantity": 10, "avg_price": 150.5}},
    "unicode": "éàü€",
}


# ---------------------------------------------------------------------------
# Test 1 : le résultat est relisible et identique au json.dumps de référence
# ---------------------------------------------------------------------------


def test_write_json_atomic_content(tmp_path: Path) -> None:
    dest = tmp_path / "state.json"
    write_json_atomic(dest, DATA)

    assert dest.exists(), "le fichier cible doit exister"
    content = dest.read_text(encoding="utf-8")
    expected = json.dumps(DATA, indent=2, ensure_ascii=False)
    assert content == expected, "contenu doit être identique au json.dumps de référence"
    # vérification relecture
    parsed = json.loads(content)
    assert parsed == DATA


# ---------------------------------------------------------------------------
# Test 2 : l'écriture passe par un .tmp puis os.replace
#          → la cible ne reçoit jamais un contenu partiel
# ---------------------------------------------------------------------------


def test_write_json_atomic_uses_tmp_then_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "broker.json"
    tmp = dest.with_suffix(dest.suffix + ".tmp")

    replace_calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:  # type: ignore[override]
        replace_calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    write_json_atomic(dest, DATA)

    assert len(replace_calls) == 1, "os.replace doit être appelé exactement une fois"
    src_used, dst_used = replace_calls[0]
    assert src_used == str(tmp), "source doit être le .tmp"
    assert dst_used == str(dest), "destination doit être le fichier cible"
    # après remplacement, le .tmp ne doit plus traîner
    assert not tmp.exists(), "le .tmp ne doit plus exister après os.replace"


# ---------------------------------------------------------------------------
# Test 3 : un tmp résiduel (crash simulé) ne corrompt pas la cible existante
# ---------------------------------------------------------------------------


def test_residual_tmp_does_not_corrupt_target(tmp_path: Path) -> None:
    dest = tmp_path / "state.json"
    original_data = {"cash": 50_000.0}
    # écriture initiale valide
    write_json_atomic(dest, original_data)

    # simulation : laisser un .tmp corrompu sans appeler os.replace
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text("CORRUPT", encoding="utf-8")

    # la cible originale est intacte
    content = dest.read_text(encoding="utf-8")
    assert json.loads(content) == original_data, "la cible doit rester intacte malgré le .tmp résiduel"


# ---------------------------------------------------------------------------
# Test 4 : le répertoire parent est créé automatiquement
# ---------------------------------------------------------------------------


def test_write_json_atomic_creates_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "deep" / "state.json"
    write_json_atomic(dest, DATA)
    assert dest.exists(), "le fichier doit être créé même si les parents n'existent pas"


# ---------------------------------------------------------------------------
# Test 5 : exception d'écriture → warning loggué, exception re-raised, cible non créée
# ---------------------------------------------------------------------------


def test_write_json_atomic_reraises_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "boom.json"

    def bad_write(path: str, data: str, encoding: str = "utf-8") -> None:
        raise OSError("disk full")

    import trader.state_db.shadow as shadow_mod

    monkeypatch.setattr(shadow_mod, "_write_text", bad_write)

    with pytest.raises(OSError, match="disk full"):
        write_json_atomic(dest, DATA)

    # la cible ne doit pas avoir été créée
    assert not dest.exists()
