"""Extraction d'objets JSON depuis une sortie modèle éventuellement entourée de prose."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

JsonObjectPredicate = Callable[[dict[str, Any]], bool]


def extract_json_object(
    text: str,
    predicate: JsonObjectPredicate | None = None,
    *,
    last: bool = True,
) -> dict[str, Any] | None:
    """Scan glissant via ``raw_decode``.  ``last=True`` retient le dernier match.

    Quand ``last`` est vrai, un texte qui est déjà du JSON valide se comporte
    comme les extracteurs historiques : un objet top-level est renvoyé même s'il
    échoue le prédicat ; un JSON non-objet (tableau, scalaire) n'est pas scanné.
    ``last=False`` ne fait que le scan et rend le premier objet accepté — plus
    tolérant qu'un slice ``find('{')`` / ``rfind('}')``.
    """

    source = text if isinstance(text, str) else str(text or "")
    if last:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = None
        else:
            return payload if isinstance(payload, dict) else None

    decoder = json.JSONDecoder()
    candidate: dict[str, Any] | None = None
    for index, char in enumerate(source):
        if char != "{":
            continue
        try:
            decoded, _end = decoder.raw_decode(source[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue
        if predicate is not None and not predicate(decoded):
            continue
        if not last:
            return decoded
        candidate = decoded
    return candidate
