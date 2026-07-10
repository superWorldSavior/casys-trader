"""Prompt and strict response parser for the company micro analyst."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from trader.application.analyst.company_micro import CompanyMicroAnalysisRequest
from trader.domain.company import CompanyIntelligenceBrief, company_brief_id

FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {
        "add",
        "remove",
        "selected",
        "selected_hotlist",
        "order",
        "orders",
        "quantity",
        "qty",
        "size",
        "sizing",
        "stop",
        "hard_stop",
        "take_profit",
    }
)


def build_company_micro_prompt(request: CompanyMicroAnalysisRequest) -> str:
    source_catalog = request.evidence.source_catalog()
    payload = {
        "symbol": request.symbol,
        "as_of": request.as_of,
        "depth": request.depth,
        "trigger": request.trigger,
        "candidate_scope_ids": list(request.candidate_scope_ids),
        "input_refs": request.input_refs or {},
        "issuer_identity": request.evidence.identity.to_dict(),
        "coverage": dict(request.evidence.coverage),
        "evidence_items": [item.to_dict() for item in request.evidence.items],
        "source_catalog": source_catalog,
    }
    return (
        "Tu es l'analyste micro entreprise de Casys Trader.\n"
        "Tu analyses UNE entreprise à partir d'évidences normalisées et sourcées. "
        "Tu ne sélectionnes jamais la hotlist et tu ne produis jamais d'ordre, quantité, stop ou sizing.\n"
        "Sépare la qualité de l'entreprise de l'attractivité du titre. Sans prix/attentes suffisants, "
        "security_readiness doit rester not_evaluated ou not_decision_grade.\n"
        "Retourne uniquement un objet JSON avec: business, financial_snapshot, earnings_and_guidance, "
        "company_thesis, catalysts, risks, open_questions, selection_view, security_readiness, source_refs.\n"
        "Chaque résumé et point doit citer dans source_refs uniquement des clés exactes de source_catalog. "
        "N'invente aucun chiffre absent des evidence_items.\n"
        "selection_view.posture vaut supports_selection|neutral|argues_against|insufficient_evidence; "
        "c'est un avis de surveillance, jamais selected=true ni BUY/SELL.\n"
        "Schéma point: {point, source_refs, evidence_label, confidence: high|medium|low, period?, horizon?}.\n"
        "JSON d'entrée:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}"
    )


def parse_company_micro_completion(
    text: str,
    *,
    request: CompanyMicroAnalysisRequest,
) -> tuple[CompanyIntelligenceBrief | None, str | None]:
    payload, error = _extract_last_json_object(text)
    if payload is None:
        return None, error or "invalid_json"
    forbidden = sorted(_find_forbidden_fields(payload))
    if forbidden:
        return None, f"forbidden_authority_fields:{','.join(forbidden)}"

    payload["brief_id"] = company_brief_id(
        request.symbol,
        request.evidence.input_signature,
        depth=request.depth,
    )
    payload["symbol"] = request.symbol
    payload["as_of"] = request.as_of
    payload["input_signature"] = request.evidence.input_signature
    payload["depth"] = request.depth
    payload["issuer_identity"] = request.evidence.identity.to_dict()
    payload["coverage"] = dict(request.evidence.coverage)
    brief = CompanyIntelligenceBrief.from_mapping(payload)
    if brief is None:
        return None, "invalid_payload"
    return brief, None


def _find_forbidden_fields(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            if key in FORBIDDEN_AUTHORITY_FIELDS:
                found.add(key)
            found.update(_find_forbidden_fields(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_find_forbidden_fields(nested))
    return found


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
        if isinstance(payload, dict):
            candidate = payload
    return (candidate, None) if candidate is not None else (None, first_error)


__all__ = [
    "FORBIDDEN_AUTHORITY_FIELDS",
    "build_company_micro_prompt",
    "parse_company_micro_completion",
]
