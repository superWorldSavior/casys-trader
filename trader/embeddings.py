"""Client embeddings OpenAI minimal — pattern _post_json, zéro dépendance nouvelle.

Utilise le même transport urllib que trader/llm.py (injectable pour les tests).
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from trader.llm import _post_json as _default_post_json

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 1536

# Timeout générique pour les appels embeddings (embedding = rapide)
_DEFAULT_TIMEOUT_S = 30


def embed_texts(
    texts: list[str],
    *,
    api_key: str,
    model: str = DEFAULT_EMBEDDING_MODEL,
    base_url: str = "https://api.openai.com/v1",
    post_json: Callable[[str, dict, dict, int], dict] | None = None,
    batch_size: int = 512,
) -> list[bytes]:
    """Encode une liste de textes via l'API OpenAI embeddings.

    Chaque texte est transformé en un blob float32 little-endian de
    EMBEDDING_DIMS × 4 octets (1536 floats32 = 6 144 octets pour
    text-embedding-3-small).

    L'ordre de sortie correspond à l'ordre d'entrée (re-tri par index API).
    Les textes sont envoyés en batches de ``batch_size`` pour limiter la taille
    des requêtes.

    Erreur réseau/HTTP → lève RuntimeError (l'appelant décide de la suite).
    """
    if post_json is None:
        post_json = _default_post_json

    url = f"{base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    blobs: list[bytes] = [b""] * len(texts)

    # Indice global dans la liste d'entrée (pour re-construire l'ordre)
    global_offset = 0

    for batch_start in range(0, len(texts), batch_size):
        batch = texts[batch_start : batch_start + batch_size]

        payload = {"model": model, "input": batch}

        try:
            response = post_json(url, payload, headers, _DEFAULT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — frontière fournisseur
            raise RuntimeError(
                f"embed_texts: échec appel embeddings ({type(exc).__name__}: {exc})"
            ) from exc

        # Trier par index pour respecter l'ordre d'entrée (l'API peut renvoyer
        # les embeddings dans un ordre quelconque)
        items = sorted(response["data"], key=lambda d: d["index"])

        for item in items:
            vec = np.asarray(item["embedding"], dtype=np.float32)
            blobs[global_offset] = vec.tobytes()
            global_offset += 1

    return blobs
