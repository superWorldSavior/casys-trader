"""Tests pour trader/learnings/embeddings.py — TDD Task 1 (learnings-recall-v1)."""

from __future__ import annotations

import numpy as np
import pytest

from trader.learnings.embeddings import EMBEDDING_DIMS, embed_texts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vec(seed: float) -> list[float]:
    """Vecteur de EMBEDDING_DIMS valeurs flottantes déterministes."""
    return [seed + i * 0.001 for i in range(EMBEDDING_DIMS)]


def _make_blob(seed: float) -> bytes:
    vec = np.asarray(_make_vec(seed), dtype=np.float32)
    return vec.tobytes()


def _stub_post_json_single_batch(url, payload, headers, timeout_s):
    """Simule une réponse OpenAI pour un seul batch (tous les textes)."""
    assert "embeddings" in url
    assert payload["model"] == "text-embedding-3-small"
    inputs = payload["input"]
    data = [{"index": i, "embedding": _make_vec(float(i))} for i in range(len(inputs))]
    return {"data": data}


# ---------------------------------------------------------------------------
# Test 1 : 2 textes → 2 blobs de la bonne taille, ordre préservé
# ---------------------------------------------------------------------------

def test_embed_texts_renvoie_deux_blobs_corrects() -> None:
    """2 textes → 2 blobs float32 LE de EMBEDDING_DIMS*4 octets, ordre préservé."""
    result = embed_texts(
        ["premier texte", "second texte"],
        api_key="test-key",
        post_json=_stub_post_json_single_batch,
    )

    assert len(result) == 2
    assert all(isinstance(b, bytes) for b in result)
    assert all(len(b) == EMBEDDING_DIMS * 4 for b in result)

    # Vérifier le contenu : texte[0] → index 0 → seed 0.0
    expected_0 = _make_blob(0.0)
    expected_1 = _make_blob(1.0)
    assert result[0] == expected_0
    assert result[1] == expected_1


# ---------------------------------------------------------------------------
# Test 2 : l'ordre de la réponse API (index) ne doit pas tromper
# ---------------------------------------------------------------------------

def _stub_post_reversed(url, payload, headers, timeout_s):
    """Réponse avec les embeddings dans l'ordre inversé (index différent de position)."""
    inputs = payload["input"]
    n = len(inputs)
    data = [{"index": i, "embedding": _make_vec(float(i))} for i in range(n - 1, -1, -1)]
    return {"data": data}


def test_embed_texts_ordonne_par_index() -> None:
    """L'ordre de sortie suit l'index de l'API, pas la position dans la réponse."""
    result = embed_texts(
        ["a", "b", "c"],
        api_key="test-key",
        post_json=_stub_post_reversed,
    )
    assert len(result) == 3
    assert result[0] == _make_blob(0.0)
    assert result[1] == _make_blob(1.0)
    assert result[2] == _make_blob(2.0)


# ---------------------------------------------------------------------------
# Test 3 : batching — batch_size=1 → 2 appels pour 2 textes
# ---------------------------------------------------------------------------

def test_embed_texts_batch_size_1_fait_deux_appels() -> None:
    """batch_size=1 → N appels réseau pour N textes, résultat final concaténé."""
    calls: list[dict] = []

    def _stub_counting(url, payload, headers, timeout_s):
        calls.append({"input": list(payload["input"])})
        idx = len(calls) - 1  # chaque batch est un seul texte
        return {"data": [{"index": 0, "embedding": _make_vec(float(idx))}]}

    result = embed_texts(
        ["texte A", "texte B"],
        api_key="test-key",
        post_json=_stub_counting,
        batch_size=1,
    )

    assert len(calls) == 2, f"Attendu 2 appels, obtenu {len(calls)}"
    assert calls[0]["input"] == ["texte A"]
    assert calls[1]["input"] == ["texte B"]
    assert len(result) == 2
    assert all(len(b) == EMBEDDING_DIMS * 4 for b in result)


# ---------------------------------------------------------------------------
# Test 4 : erreur réseau → RuntimeError
# ---------------------------------------------------------------------------

def _stub_raises(url, payload, headers, timeout_s):
    raise RuntimeError("connexion refusée")


def test_embed_texts_erreur_reseau_leve_runtime_error() -> None:
    with pytest.raises(RuntimeError):
        embed_texts(["x"], api_key="key", post_json=_stub_raises)


# ---------------------------------------------------------------------------
# Test 5 : header Authorization correct
# ---------------------------------------------------------------------------

def test_embed_texts_envoie_bearer_token() -> None:
    captured: list[dict] = []

    def _stub_capture(url, payload, headers, timeout_s):
        captured.append({"url": url, "headers": dict(headers)})
        return {"data": [{"index": 0, "embedding": _make_vec(0.0)}]}

    embed_texts(["hello"], api_key="sk-testkey", post_json=_stub_capture)

    assert len(captured) == 1
    assert captured[0]["headers"].get("Authorization") == "Bearer sk-testkey"
    assert "embeddings" in captured[0]["url"]
