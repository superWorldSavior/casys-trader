"""Prompt and JSON extraction for the macro/news analyst."""

from __future__ import annotations

import json
from typing import Any

from trader.application.analyst import NewsMacroAnalysisRequest
from trader.domain.situation import NewsMacroBrief


def build_news_macro_prompt(request: NewsMacroAnalysisRequest) -> str:
    payload = {
        "as_of": request.as_of,
        "valid_until": request.valid_until,
        "news_items": list(request.news_items),
        "macro_next": list(request.macro_next),
        "candidate_symbols": list(request.candidate_symbols),
    }
    return (
        "Tu es l'analyste macro/news de Casys Trader.\n"
        "Lis uniquement le JSON fourni. Distille les signaux forts/faibles utiles "
        "pour la selection d'univers et le contexte de trading.\n"
        "Retourne uniquement un objet JSON valide avec les cles: as_of, valid_until, "
        "zones, families, symbols, alerts.\n"
        "Contraintes: <=5 points par zone, <=3 par famille/symbole, <=8 alerts, "
        "point <=200 caracteres, chaque point cite ses uuids sources quand ils existent.\n"
        "Schema point: {point, sources, symbols, severity: info|watch|risk, "
        "signal: weak|strong|event, horizon?}.\n"
        "JSON d'entree:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def parse_news_macro_completion(
    text: str,
    *,
    as_of: str,
    valid_until: str,
) -> tuple[NewsMacroBrief | None, str | None]:
    payload, error = _extract_last_json_object(text)
    if payload is None:
        return None, error or "invalid_json"
    payload.setdefault("as_of", as_of)
    payload.setdefault("valid_until", valid_until)
    brief = NewsMacroBrief.from_mapping(payload)
    if brief is None:
        return None, "invalid_payload"
    return brief, None


def _extract_last_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        first_error = str(exc)
    else:
        return (payload, None) if isinstance(payload, dict) else (None, "json_payload_not_object")

    decoder = json.JSONDecoder()
    candidate: dict[str, Any] | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and _looks_like_news_macro_payload(payload):
            candidate = payload
    if candidate is not None:
        return candidate, None
    return None, first_error


def _looks_like_news_macro_payload(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("zones", "families", "symbols", "alerts", "as_of", "valid_until"))
