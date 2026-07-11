"""Prompt and JSON extraction for the macro/news analyst."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from trader.application.analyst import NewsMacroAnalysisRequest
from trader.domain.situation import NewsMacroBrief


def build_news_macro_source_catalog(request: NewsMacroAnalysisRequest) -> dict[str, str]:
    """Map stable input references to concise operator-readable source names."""

    catalog: dict[str, str] = {}
    for item in (*request.news_items, *request.global_news_items):
        ref = str(item.get("uuid") or "").strip()
        if not ref:
            continue
        label = _news_source_label(item)
        if label:
            catalog[ref] = label
    for item in request.macro_series:
        label = str(item.get("label") or "").strip()
        if label:
            catalog[f"macro_series:{label}"] = f"DBnomics · {label.replace('_', ' ')}"
    for item in request.geopolitical_events:
        ref = str(item.get("url") or "").strip()
        if ref:
            country = str(item.get("sourcecountry") or "").strip()
            catalog[ref] = f"GDELT · {country}" if country else "GDELT"
    for item in request.macro_next:
        event = str(item.get("event") or "").strip()
        at = str(item.get("at") or "").strip()
        if event:
            ref = f"macro_next:{event}:{at}" if at else f"macro_next:{event}"
            catalog[ref] = f"Macro calendar · {event}"
    if request.venue:
        catalog[f"shortlist:{request.venue}"] = f"Heuristic shortlist · {request.venue}"
    for symbol, anchor in (request.company_anchors or {}).items():
        ref = str(anchor.get("anchor_ref") or "").strip()
        if ref:
            catalog[ref] = f"Company micro brief · {symbol}"
    return catalog


def _news_source_label(item: dict) -> str:
    for key in ("publisher", "source", "provider"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    link = str(item.get("link") or "").strip()
    if link:
        hostname = (urlparse(link).hostname or "").removeprefix("www.")
        if hostname:
            return hostname
    return ""


def build_news_macro_prompt(request: NewsMacroAnalysisRequest) -> str:
    source_catalog = build_news_macro_source_catalog(request)
    payload = {
        "as_of": request.as_of,
        "valid_until": request.valid_until,
        "venue": request.venue,
        "input_refs": request.input_refs or {},
        "news_items": list(request.news_items),
        "global_news_items": list(request.global_news_items),
        "macro_next": list(request.macro_next),
        "macro_series": list(request.macro_series),
        "geopolitical_events": list(request.geopolitical_events),
        "candidate_symbols": list(request.candidate_symbols),
        "family_context": request.family_context or {},
        "company_anchors": request.company_anchors or {},
        "source_catalog": source_catalog,
    }
    return (
        "Tu es l'analyste macro/news de Casys Trader.\n"
        "Lis uniquement le JSON fourni. Distille les signaux forts/faibles utiles "
        "pour la selection d'univers et le contexte de trading.\n"
        "Retourne uniquement un objet JSON valide avec les cles: brief_id, venue, "
        "as_of, valid_until, input_refs, zones, families, symbols, alerts.\n"
        "Contraintes: <=5 points par zone, <=3 par famille/symbole, <=8 alerts, "
        "point <=200 caracteres. Dans sources, mets uniquement les noms lisibles "
        "du source_catalog; ne mets jamais un UUID. Dans source_refs, cite uniquement "
        "les cles exactes du source_catalog qui justifient le point.\n"
        "Les company_anchors sont des ancres micro durables: utilise-les seulement "
        "pour dire si une news confirme, infirme ou change une these existante. "
        "Ne modifie jamais ces ancres et ne les traite pas comme un ordre.\n"
        "geopolitical_events (GDELT) et global_news_items decrivent la situation "
        "geopolitique et macro internationale. En portee GLOBAL (venue=GLOBAL), "
        "privilegie des zones et alertes transverses: banques centrales, taux, USD, "
        "commodites (petrole, or), tensions, sanctions, conflits, elections; distingue "
        "signal faible/fort et direction risk_on/risk_off. N'invente aucune donnee absente.\n"
        "Schema point: {point, sources, source_refs, symbols, severity: info|watch|risk, "
        "signal: weak|strong|event, direction?: bullish|bearish|risk_on|risk_off|neutral|mixed, horizon?}.\n"
        "JSON d'entree:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def parse_news_macro_completion(
    text: str,
    *,
    as_of: str,
    valid_until: str,
    venue: str = "GLOBAL",
    input_refs: dict | None = None,
) -> tuple[NewsMacroBrief | None, str | None]:
    payload, error = _extract_last_json_object(text)
    if payload is None:
        return None, error or "invalid_json"
    # The model owns the analysis body, never the audit envelope.
    payload["as_of"] = as_of
    payload["valid_until"] = valid_until
    payload["venue"] = venue
    payload["input_refs"] = input_refs or {}
    payload["brief_id"] = f"{as_of}|{venue or 'GLOBAL'}"
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
