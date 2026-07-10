"""Prompt and response parsing for the universe-composition agent."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from trader.application.universe import UniverseAgentDecision, UniverseCompositionRequest


def build_universe_prompt(request: UniverseCompositionRequest) -> str:
    """Render the complete bounded venue request for the universe agent."""

    payload = request.to_dict()
    return (
        "Tu es l'agent univers de Casys Trader.\n"
        "Ta responsabilité exclusive: compose toi-même la hotlist complète "
        "de cette venue. La baseline est une référence et un fallback technique, "
        "pas la décision nominale.\n"
        "Choisis entre 1 et 25 symboles non-sticky, uniquement dans candidates. "
        "Les sticky seront ajoutés après ta sélection et ne consomment aucune place.\n"
        "Utilise explicitement le contexte GLOBAL/zones, toutes les familles, les "
        "situations symbole, le contexte entreprise quand il est fourni, le régime et le "
        "snapshot famille; explique chaque symbole retenu "
        "et les arbitrages importants entre signaux. N'invente aucune information absente.\n"
        "Le global_family_board compare les opportunités famille entre TW/EU/US. "
        "Utilise-le comme contexte relatif, jamais comme quota de places, allocation de capital "
        "ou instruction de copier la sélection d'une autre venue.\n"
        "Retourne uniquement un objet JSON valide, jamais un simple add/remove, avec ce schéma:\n"
        '{"selected_hotlist":["SYMBOL"],"summary":"...",'
        '"family_postures":{"family":"..."},'
        '"symbol_rationales":{"SYMBOL":"..."},'
        '"symbol_mandates":{"SYMBOL":{"why_selected":"...","role":"...",'
        '"posture":"...","allowed_sides":["long","short"]}}}\n'
        "Contraintes: selected_hotlist non vide, 25 maximum, aucun sticky, aucun symbole "
        "hors candidates; une rationale et un symbol_mandate non vides pour chaque symbole "
        "sélectionné. Un mandat est un contexte de surveillance, jamais un ordre: aucun qty, "
        "stop, sizing ou obligation de trader.\n"
        "JSON d'entrée borné:\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def parse_universe_completion(
    text: str,
    *,
    baseline: tuple[str, ...] | list[str],
) -> tuple[UniverseAgentDecision | None, str | None]:
    """Parse the full contract or the historical ``add/remove`` delta."""

    payload, error = _extract_last_json_object(text)
    if payload is None:
        return None, error or "invalid_json"

    if "selected_hotlist" in payload:
        selected, list_error = _string_list(payload.get("selected_hotlist"), "selected_hotlist")
        if list_error:
            return None, list_error
        if "summary" not in payload:
            return None, "summary_missing"
        if not isinstance(payload.get("summary"), str):
            return None, "summary_not_string"
        if "family_postures" not in payload:
            return None, "family_postures_missing"
        family_postures, mapping_error = _string_mapping(payload.get("family_postures"), "family_postures")
        if mapping_error:
            return None, mapping_error
        if "symbol_rationales" not in payload:
            return None, "symbol_rationales_missing"
        rationales, mapping_error = _string_mapping(
            payload.get("symbol_rationales"),
            "symbol_rationales",
        )
        if mapping_error:
            return None, mapping_error
        mandates, mandate_error = _symbol_mandates(payload.get("symbol_mandates"))
        if mandate_error:
            return None, mandate_error
        summary = str(payload.get("summary") or "").strip()
        return (
            UniverseAgentDecision(
                selected_hotlist=tuple(selected or ()),
                summary=summary,
                family_postures=family_postures or {},
                symbol_rationales=rationales or {},
                contract_version="universe.v2" if "symbol_mandates" in payload else "universe.v1",
                symbol_mandates=mandates or {},
            ),
            None,
        )

    if "add" in payload or "remove" in payload:
        add, list_error = _string_list(payload.get("add", []), "add")
        if list_error:
            return None, list_error
        remove, list_error = _string_list(payload.get("remove", []), "remove")
        if list_error:
            return None, list_error
        selected = _apply_legacy_delta(baseline, add=add or [], remove=remove or [])
        supplied_rationales, mapping_error = _string_mapping(
            payload.get("symbol_rationales", {}),
            "symbol_rationales",
        )
        if mapping_error:
            return None, mapping_error
        rationales = {
            symbol: (supplied_rationales or {}).get(
                symbol,
                "Selected by legacy add/remove compatibility contract.",
            )
            for symbol in selected
        }
        family_postures, mapping_error = _string_mapping(payload.get("family_postures", {}), "family_postures")
        if mapping_error:
            return None, mapping_error
        return (
            UniverseAgentDecision(
                selected_hotlist=tuple(selected),
                summary=str(payload.get("summary") or "Legacy add/remove compatibility response.").strip(),
                family_postures=family_postures or {},
                symbol_rationales=rationales,
                contract_version="legacy.add_remove.v1",
            ),
            None,
        )

    return None, "missing_selected_hotlist_or_legacy_delta"


def _extract_last_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        first_error = "invalid_json"
    else:
        return (payload, None) if isinstance(payload, dict) else (None, "json_payload_not_object")

    decoder = json.JSONDecoder()
    candidate: dict[str, Any] | None = None
    for index, char in enumerate(str(text or "")):
        if char != "{":
            continue
        try:
            decoded, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and _looks_like_universe_payload(decoded):
            candidate = decoded
    if candidate is not None:
        return candidate, None
    return None, first_error


def _symbol_mandates(value: Any) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    if value is None:
        return {}, None
    if not isinstance(value, Mapping):
        return None, "symbol_mandates_not_mapping"
    forbidden = {"order", "orders", "qty", "quantity", "stop", "sizing", "size", "risk_pct"}
    result: dict[str, dict[str, Any]] = {}
    for raw_symbol, raw_mandate in value.items():
        symbol = str(raw_symbol or "").strip()
        if not symbol or not isinstance(raw_mandate, Mapping):
            return None, "invalid_symbol_mandate"
        if forbidden.intersection(str(key).strip().lower() for key in raw_mandate):
            return None, f"forbidden_symbol_mandate_field:{symbol}"
        allowed_sides = raw_mandate.get("allowed_sides") or ()
        if not isinstance(allowed_sides, list) or any(side not in {"long", "short"} for side in allowed_sides):
            return None, f"invalid_allowed_sides:{symbol}"
        result[symbol] = {
            "why_selected": str(raw_mandate.get("why_selected") or "").strip()[:500],
            "role": str(raw_mandate.get("role") or "monitor").strip()[:80],
            "posture": str(raw_mandate.get("posture") or "neutral").strip()[:120],
            "allowed_sides": list(dict.fromkeys(allowed_sides)),
        }
    return result, None


def _looks_like_universe_payload(payload: Mapping[str, Any]) -> bool:
    return "selected_hotlist" in payload or "add" in payload or "remove" in payload


def _string_list(value: Any, field: str) -> tuple[list[str] | None, str | None]:
    if not isinstance(value, list):
        return None, f"{field}_not_list"
    if any(not isinstance(item, str) for item in value):
        return None, f"{field}_contains_non_string"
    return [item.strip() for item in value], None


def _string_mapping(value: Any, field: str) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(value, Mapping):
        return None, f"{field}_not_object"
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        return None, f"{field}_contains_non_string"
    return {key.strip(): item.strip() for key, item in value.items()}, None


def _apply_legacy_delta(
    baseline: tuple[str, ...] | list[str],
    *,
    add: list[str],
    remove: list[str],
) -> list[str]:
    removals = set(remove)
    selected: list[str] = []
    for raw_symbol in baseline:
        symbol = str(raw_symbol or "").strip()
        if symbol and symbol not in removals and symbol not in selected:
            selected.append(symbol)
    for symbol in add:
        if symbol and symbol not in selected:
            selected.append(symbol)
    return selected
