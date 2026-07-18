"""Bounded company-intelligence tools for the universe agent."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from trader.agent.tools.core import AgentToolCall, ToolSpec

_ALLOWED_SECTIONS = (
    "thesis",
    "catalysts",
    "risks",
    "financial",
    "earnings",
    "business",
)
_DEFAULT_MAX_CHARS = 1200
_MIN_MAX_CHARS = 200
_MAX_MAX_CHARS = 4000


def _validate_args(args: dict[str, Any]) -> str | None:
    symbols = args.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 10:
        return "symbols doit être une liste non vide de 1 à 10 symboles"
    if any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
        return "symbols doit contenir uniquement des strings non vides"

    sections = args.get("sections", list(_ALLOWED_SECTIONS))
    if not isinstance(sections, list) or any(not isinstance(section, str) for section in sections):
        return "sections doit être une liste de strings"
    unknown = [section for section in sections if section not in _ALLOWED_SECTIONS]
    if unknown:
        return f"sections inconnues: {','.join(unknown)}"

    max_chars = args.get("max_chars", _DEFAULT_MAX_CHARS)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        return "max_chars doit être un entier"
    if not _MIN_MAX_CHARS <= max_chars <= _MAX_MAX_CHARS:
        return f"max_chars doit être entre {_MIN_MAX_CHARS} et {_MAX_MAX_CHARS}"
    return None


def _section_payloads(brief: Any) -> dict[str, Any]:
    return {
        "thesis": brief.company_thesis.to_dict(),
        "catalysts": [point.to_dict() for point in brief.catalysts],
        "risks": [point.to_dict() for point in brief.risks],
        "financial": brief.financial_snapshot.to_dict(),
        "earnings": brief.earnings_and_guidance.to_dict(),
        "business": brief.business.to_dict(),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fit_json_value(value: Any, budget: int) -> Any:
    """Keep the exact JSON value when possible, otherwise return a bounded JSON excerpt."""

    encoded = _json(value)
    if len(encoded) <= budget:
        return value
    if budget <= 2:
        return ""

    low = 0
    high = len(encoded)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        excerpt = encoded[:middle].rstrip()
        if middle < len(encoded) and excerpt:
            excerpt += "…"
        if len(_json(excerpt)) <= budget:
            best = excerpt
            low = middle + 1
        else:
            high = middle - 1
    return best


def _bounded_sections(
    payloads: Mapping[str, Any],
    *,
    sections: list[str],
    max_chars: int,
) -> dict[str, Any]:
    if not sections:
        return {}
    wrapper_chars = 2 + max(0, len(sections) - 1)
    wrapper_chars += sum(len(_json(section)) + 1 for section in sections)
    available = max(0, max_chars - wrapper_chars)
    base, remainder = divmod(available, len(sections))
    bounded = {
        section: _fit_json_value(payloads[section], base + (index < remainder))
        for index, section in enumerate(sections)
    }
    return bounded


def make_get_company_briefs_spec(intelligence_store: Any) -> ToolSpec:
    """Build the universe-only company brief tool around a duck-typed store."""

    def handler(call: AgentToolCall, context: Any) -> dict[str, Any]:
        store = getattr(context, "intelligence_store", intelligence_store)
        symbols = [symbol.strip() for symbol in call.args["symbols"]]
        sections = list(call.args.get("sections", _ALLOWED_SECTIONS))
        max_chars = int(call.args.get("max_chars", _DEFAULT_MAX_CHARS))
        briefs = store.read_current_many(symbols, depth="preferred")
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            brief = briefs.get(symbol)
            if brief is None:
                rows.append({"symbol": symbol, "error": "not_found"})
                continue
            selection = brief.selection_view.to_dict()
            rows.append(
                {
                    "symbol": symbol,
                    "as_of": brief.as_of,
                    "brief_ref": brief.ref(),
                    "selection_view": {
                        "posture": selection.get("posture"),
                        "confidence": selection.get("confidence"),
                    },
                    "sections": _bounded_sections(
                        _section_payloads(brief),
                        sections=sections,
                        max_chars=max_chars,
                    ),
                }
            )
        return {"rows": rows}

    return ToolSpec(name="get_company_briefs", validate_args=_validate_args, handler=handler)


__all__ = ["make_get_company_briefs_spec"]
