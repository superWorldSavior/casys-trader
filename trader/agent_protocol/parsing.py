"""Parsing helpers for agent JSON responses."""

from __future__ import annotations

import json

from trader import decision_reason
from trader.agent_protocol.types import (
    BatchToolCallRequest,
    ContextResearchRequest,
    Decision,
    IndicatorRequest,
)

_DECISION_KEYS = {"symbol", "action", "quantity", "confidence", "rationale", "decision_reason_code"}
MAX_LEARNING_CHARS = 1000  # borne la note pour ne pas faire exploser le prompt/store


def _normalize_learning(value: object) -> str | None:
    """Accepte seulement une note textuelle non vide, bornée. Sinon None."""
    if not isinstance(value, str):
        return None
    note = value.strip()
    if not note:
        return None
    return note[:MAX_LEARNING_CHARS]


def _extract_json(text: str) -> dict:
    """Récupère le 1er objet JSON du texte (Codex peut entourer de prose)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("aucun objet JSON trouvé")
    return json.loads(text[start : end + 1])


def _optional_float(data: dict, key: str) -> float | None:
    if data.get(key) is None:
        return None
    return float(data[key])


def _optional_upper_str(data: dict, key: str) -> str | None:
    if data.get(key) is None:
        return None
    return str(data[key]).upper()


def _optional_dict(data: dict, key: str) -> dict | None:
    if data.get(key) is None:
        return None
    return dict(data[key])


def _cancel_watch_ids(data: dict) -> list[str]:
    raw_ids = data.get("cancel_watch_ids")
    if not isinstance(raw_ids, list):
        return []
    return [str(wid) for wid in raw_ids if isinstance(wid, str)]


def _decision_reason_code(data: dict) -> str:
    if "decision_reason_code" not in data or data.get("decision_reason_code") is None:
        return decision_reason.infer_reason_code(data)
    return decision_reason.normalize_reason_code(data.get("decision_reason_code"))


def _decision_from_dict(data: dict, symbol: str) -> Decision:
    if not isinstance(data, dict):
        raise ValueError("élément non-objet")
    missing = _DECISION_KEYS - data.keys()
    blocking_missing = missing - {"decision_reason_code"}
    if blocking_missing:
        raise ValueError(f"clés manquantes: {blocking_missing}")
    action = str(data["action"]).upper()
    if action not in ("BUY", "SELL", "HOLD"):
        raise ValueError(f"action invalide: {action}")
    return Decision(
        symbol=str(data["symbol"]),
        action=action,  # type: ignore[arg-type]
        quantity=float(data["quantity"]),
        confidence=float(data["confidence"]),
        rationale=str(data["rationale"]),
        next_wake_in_minutes=_optional_float(data, "next_wake_in_minutes"),
        intent=_optional_upper_str(data, "intent"),  # type: ignore[arg-type]
        exit_plan=_optional_dict(data, "exit_plan"),
        indicator_watch=_optional_dict(data, "indicator_watch"),
        cancel_watch_ids=_cancel_watch_ids(data),
        learning=_normalize_learning(data.get("learning")),
        decision_reason_code=_decision_reason_code(data),
    )


def parse_decision(raw_text: str, symbol: str) -> Decision:
    return _decision_from_dict(_extract_json(raw_text), symbol)


def _indicator_requests_from(data: dict, symbol: str) -> list[IndicatorRequest]:
    raw_requests = data.get("requests") or data.get("indicator_requests") or []
    requests: list[IndicatorRequest] = []
    for item in raw_requests:
        if not isinstance(item, dict):
            continue
        indicators = item.get("indicators") or item.get("names") or item.get("indicator") or []
        if isinstance(indicators, str):
            indicators = [indicators]
        requests.append(
            IndicatorRequest(
                symbol=str(item.get("symbol") or symbol),
                indicators=[str(name) for name in indicators],
                timeframe=str(item.get("timeframe") or item.get("interval") or "1h"),
                lookback=(None if item.get("lookback") is None else str(item["lookback"])),
                window=int(item.get("window") or 48),
                as_of=str(item.get("as_of") or "latest"),
            )
        )
    return requests


def _response_from_dict(data: dict, symbol: str) -> Decision | ContextResearchRequest:
    if not isinstance(data, dict):
        raise ValueError("élément non-objet")
    action = str(data.get("action", "")).upper()
    if action in {"REQUEST_CONTEXT", "NEEDS_CONTEXT"} or data.get("needs_context") is True:
        return ContextResearchRequest(
            symbol=str(data.get("symbol") or symbol),
            rationale=str(data.get("rationale") or ""),
            requests=_indicator_requests_from(data, symbol),
            next_wake_in_minutes=_optional_float(data, "next_wake_in_minutes"),
        )
    return _decision_from_dict(data, symbol)


def parse_decision_or_context_request(raw_text: str, symbol: str) -> Decision | ContextResearchRequest:
    return _response_from_dict(_extract_json(raw_text), symbol)


def _parse_batch_data(
    data: dict, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest]:
    """Corps de parse_batch sur un dict déjà extrait. Isolation per-élément."""
    by_symbol: dict[str, Decision | ContextResearchRequest] = {}
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        # tool_calls SANS clé decisions au tour final → raison explicite (défense
        # en profondeur). Mais `decisions` présent-et-malformé reste un
        # batch_bad_output : ne pas le masquer en tool_loop_blocked.
        raw_calls = data.get("tool_calls")
        if decisions is None and isinstance(raw_calls, list) and any(isinstance(c, dict) for c in raw_calls):
            return {sym: Decision.hold(sym, "tool_loop_blocked") for sym in symbols}
        return {sym: Decision.hold(sym, "batch_bad_output") for sym in symbols}

    requested = set(symbols)
    for element in decisions:
        sym = str(element.get("symbol")) if isinstance(element, dict) else None
        if sym is None or sym not in requested:
            continue  # symbole hors périmètre ou élément non-objet -> ignoré
        try:
            if allow_context_request:
                by_symbol[sym] = _response_from_dict(element, sym)
            else:
                by_symbol[sym] = _decision_from_dict(element, sym)
        except Exception as e:  # noqa: BLE001 - isolation per-élément
            by_symbol[sym] = Decision.hold(sym, f"batch_bad_output: {e}")

    for sym in symbols:
        by_symbol.setdefault(sym, Decision.hold(sym, "missing_in_batch"))
    return by_symbol


def parse_batch(
    raw_text: str, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest]:
    """Décode un tableau de décisions (une par symbole), avec ISOLATION per-élément :
    un élément invalide -> HOLD pour CE symbole, les autres passent. Un JSON global
    invalide -> tous HOLD. Tout symbole demandé mais absent de la réponse -> HOLD."""
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError):
        return {sym: Decision.hold(sym, "batch_bad_output") for sym in symbols}
    return _parse_batch_data(data, symbols, allow_context_request=allow_context_request)


def parse_batch_or_tool_calls(
    raw_text: str, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest] | BatchToolCallRequest:
    """Réponse batch OU tournée d'outils. tool_calls non vide prime ; toute
    malformation retombe sur le chemin décisions (fail-safe HOLD)."""
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError):
        return {sym: Decision.hold(sym, "batch_bad_output") for sym in symbols}
    raw_calls = data.get("tool_calls")
    if isinstance(raw_calls, list):
        calls = [c for c in raw_calls if isinstance(c, dict)]
        if calls:
            return BatchToolCallRequest(calls=calls)
    return _parse_batch_data(data, symbols, allow_context_request=allow_context_request)
