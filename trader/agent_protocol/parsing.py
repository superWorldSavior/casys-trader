"""Parsing helpers for agent JSON responses."""

from __future__ import annotations

import json
from dataclasses import replace

from trader.reporting import decision_reason
from trader.agent_protocol.types import (
    BatchToolCallRequest,
    ContextResearchRequest,
    Decision,
    IndicatorRequest,
)

_DECISION_KEYS = {"symbol", "action", "quantity", "confidence", "rationale", "decision_reason_code"}
_LEGACY_DECISION_FIELDS = {
    "action",
    "quantity",
    "qty",
    "intent",
    "exit_plan",
    "indicator_watch",
    "cancel_watch_ids",
    "next_wake_in_minutes",
    "learning",
}
MAX_LEARNING_CHARS = 1000  # borne la note pour ne pas faire exploser le prompt/store
MAX_THESIS_FIELD_CHARS = 200  # borne chaque champ texte du thesis tag
_THESIS_VALID_HORIZONS = frozenset({"intraday", "swing", "position"})


def _normalize_thesis(value: object) -> dict | None:
    """Valide et borne le thesis tag {setup, horizon, invalidation}.

    Fail-safe : tout thesis malformé, partiel ou hors-contrat retourne None
    sans faire tomber la décision. Règles :
    - value doit être un dict
    - setup et invalidation : str non vide, tronqués à MAX_THESIS_FIELD_CHARS
    - horizon : str dans {intraday, swing, position} (case-insensitive, normalisé lowercase)
    - un seul champ manquant → None
    """
    if not isinstance(value, dict):
        return None
    setup = value.get("setup")
    horizon = value.get("horizon")
    invalidation = value.get("invalidation")
    if not isinstance(setup, str) or not isinstance(horizon, str) or not isinstance(invalidation, str):
        return None
    setup = setup.strip()[:MAX_THESIS_FIELD_CHARS]
    invalidation = invalidation.strip()[:MAX_THESIS_FIELD_CHARS]
    horizon_norm = horizon.strip().lower()
    if not setup or not invalidation or horizon_norm not in _THESIS_VALID_HORIZONS:
        return None
    return {"setup": setup, "horizon": horizon_norm, "invalidation": invalidation}


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
        amend_exit=_optional_dict(data, "amend_exit"),
    )


def _tool_call_id(symbol: str, index: int, raw: dict) -> str:
    raw_id = raw.get("id")
    return str(raw_id) if isinstance(raw_id, str) and raw_id else f"{symbol}:{index}"


def _tool_args(raw: dict) -> dict:
    args = raw.get("args")
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise ValueError("tool_args_must_be_object")
    return args


def _compact_stop(raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    if raw.get("struct") is not None:
        out = {"type": "structural", "anchor": raw["struct"]}
        for key in ("window", "buffer_pct", "buffer_atr", "min_pct", "max_pct"):
            if raw.get(key) is not None:
                out[key] = raw[key]
        return out
    if raw.get("type") == "structural" and raw.get("anchor") is None and raw.get("struct") is not None:
        out = dict(raw)
        out["anchor"] = out.pop("struct")
        return out
    return dict(raw)


def _compact_take_profits(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            if item.get("r") is not None:
                tp = {"type": "risk_multiple", "r": item["r"]}
                if item.get("fraction") is not None:
                    tp["fraction"] = item["fraction"]
                if item.get("name") is not None:
                    tp["name"] = item["name"]
                out.append(tp)
            else:
                out.append(dict(item))
        else:
            out.append({"price": item})
    return out


def _compact_trailing(raw: object) -> dict | None:
    if raw in (None, False, ""):
        return None
    if not isinstance(raw, dict):
        return {"trail_type": "price", "trail_value": raw}
    value = raw.get("trail_value")
    if value is None:
        value = raw.get("value")
    if value is None:
        return None
    trail_type = raw.get("trail_type") or raw.get("type") or "price"
    out = {"trail_type": trail_type, "trail_value": value}
    if raw.get("enabled_after") is not None:
        out["enabled_after"] = raw["enabled_after"]
    return out


def _first_present(data: dict, *keys: str) -> object | None:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def _compact_protection(raw: object) -> dict | None:
    if raw in (None, False, ""):
        return None
    if raw is True:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("protect_must_be_object")
    out: dict = {}
    arm = _first_present(raw, "arm_r", "arm_at_r", "arm_at_R", "after_r", "enabled_after_r", "activate_after_r")
    if arm is not None:
        out["arm_at_r"] = arm
    giveback = _first_present(raw, "giveback", "giveback_pct", "trigger_on_giveback_pct")
    if giveback is not None:
        out["trigger_on_giveback_pct"] = giveback
    for src, dst in (
        ("close_fraction", "close_fraction"),
        ("min_hold_minutes", "min_hold_minutes"),
        ("move_stop_to", "move_stop_to"),
    ):
        if raw.get(src) is not None:
            out[dst] = raw[src]
    lock_r = _first_present(raw, "lock_r", "protect_r", "lock_in_r")
    if lock_r is not None:
        out["lock_r"] = lock_r
    return out


def _compact_exit_plan(raw: object) -> dict | None:
    if raw in (None, False, ""):
        return None
    if not isinstance(raw, dict):
        raise ValueError("exit_must_be_object")
    out: dict = {}
    stop = _first_present(raw, "stop", "hard_stop", "sl")
    if stop is not None:
        out["hard_stop"] = _compact_stop(stop)
    take_profits = _first_present(raw, "tp", "take_profits")
    if take_profits is not None:
        out["take_profits"] = _compact_take_profits(take_profits)
    trailing = _first_present(raw, "trail", "trailing_stop")
    compact_trailing = _compact_trailing(trailing)
    if compact_trailing is not None:
        out["trailing_stop"] = compact_trailing
    protection = _first_present(raw, "protect", "profit_protection")
    compact_protection = _compact_protection(protection)
    if compact_protection is not None:
        out["profit_protection"] = compact_protection
    if raw.get("exit_watch") is not None:
        out["exit_watch"] = raw["exit_watch"]
    max_hold = _first_present(raw, "max_hold_minutes", "max_hold_m")
    if max_hold is not None:
        out["max_hold_minutes"] = max_hold
    return out or None


def _action_for_order_tool(args: dict) -> str:
    raw_action = args.get("side") or args.get("action")
    if raw_action is not None:
        action = str(raw_action).upper()
        if action in {"BUY", "SELL"}:
            return action
        raise ValueError("order_side_invalid")
    intent = str(args.get("intent") or "").upper()
    if intent == "OPEN_LONG":
        return "BUY"
    if intent == "OPEN_SHORT":
        return "SELL"
    raise ValueError("order_side_required")


def _decision_from_symbol_calls(data: dict, symbol: str) -> Decision:
    if any(field in data for field in _LEGACY_DECISION_FIELDS):
        raise ValueError("mixed_legacy_and_tools")
    calls = data.get("calls")
    if not isinstance(calls, list):
        raise ValueError("calls_must_be_list")

    decision: dict = {
        "symbol": symbol,
        "action": "HOLD",
        "quantity": 0.0,
        "confidence": float(data.get("confidence") or 0.0),
        "rationale": str(data.get("rationale") or ""),
        "intent": "HOLD",
        "decision_reason_code": _decision_reason_code(data),
    }
    traces: list[dict] = []
    cancel_ids: list[str] = []
    next_wake_event: str | None = None
    # L1 — sizing en risque : extrait de propose_order.args{"risk_pct":...}
    risk_pct_target_local: float | None = None

    for index, raw in enumerate(calls):
        if not isinstance(raw, dict):
            raise ValueError("tool_call_must_be_object")
        tool = raw.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ValueError("tool_name_required")
        args = _tool_args(raw)
        call_id = _tool_call_id(symbol, index, raw)
        traces.append({"id": call_id, "tool": tool, "args": args, "outcome": "ok", "detail": {}})

        if tool == "propose_order":
            intent = str(args.get("intent") or "").upper()
            if intent not in {"OPEN_LONG", "OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE", "ADD"}:
                raise ValueError("order_intent_invalid")

            needs_position_resolve = False
            reduce_fraction: float | None = None
            try:
                action = _action_for_order_tool(args)
            except ValueError as exc:
                if str(exc) == "order_side_required" and intent in {"CLOSE", "REDUCE", "REVERSE", "ADD"}:
                    needs_position_resolve = True
                    action = "HOLD"  # Provisoire — remplacé par le daemon depuis la position
                    if intent == "REDUCE":
                        frac = args.get("fraction")
                        if frac is not None:
                            reduce_fraction = float(frac)
                else:
                    raise

            qty_raw = args.get("qty") if args.get("qty") is not None else args.get("quantity")
            if needs_position_resolve:
                if intent == "CLOSE":
                    qty = 0.0  # Dérivé de |position| dans le daemon
                elif intent == "REDUCE":
                    qty = float(qty_raw) if qty_raw is not None else 0.0
                else:  # REVERSE / ADD : qty requise (jambe cible ou renforcement)
                    if qty_raw is None:
                        raise ValueError("order_qty_required")
                    qty = float(qty_raw)
            else:
                # L1 — risk_pct est une alternative à qty pour OPEN_LONG/OPEN_SHORT.
                # Explicit qty > risk_pct (Explicit Over Implicit).
                risk_pct_raw = args.get("risk_pct")
                if qty_raw is None and risk_pct_raw is None:
                    raise ValueError("order_qty_required")
                if qty_raw is not None:
                    qty = float(qty_raw)
                else:
                    # qty absente : placeholder 0.0 ; daemon dérive la qty réelle
                    # depuis risk_pct_target × equity / (stop_distance × fx_rate).
                    qty = 0.0
                    risk_pct_target_local = float(risk_pct_raw)

            decision["action"] = action
            decision["quantity"] = qty
            decision["intent"] = intent
            decision["exit_plan"] = _compact_exit_plan(args.get("exit"))
            decision["thesis"] = _normalize_thesis(args.get("thesis"))
            if needs_position_resolve:
                decision["_resolve_from_position"] = True
                if reduce_fraction is not None:
                    decision["_reduce_fraction"] = reduce_fraction
        elif tool == "set_next_wake":
            on_event = args.get("on")
            minutes = args.get("minutes")
            if on_event is not None:
                # Réveil événementiel : stocké pour résolution au daemon.
                # Un on: inconnu est accepté ici ; le daemon tombera en fail-safe.
                next_wake_event = str(on_event)
            elif minutes is not None:
                decision["next_wake_in_minutes"] = float(minutes)
            else:
                raise ValueError("wake_minutes_required")
        elif tool == "record_learning":
            decision["learning"] = _normalize_learning(args.get("note"))
        elif tool == "propose_indicator_watch":
            watch = args.get("watch")
            decision["indicator_watch"] = dict(watch) if isinstance(watch, dict) else dict(args)
        elif tool == "cancel_watch":
            raw_ids = args.get("ids") or args.get("watch_ids")
            if raw_ids is None and isinstance(args.get("id"), str):
                raw_ids = [args["id"]]
            if not isinstance(raw_ids, list):
                raise ValueError("cancel_watch_ids_required")
            cancel_ids.extend(str(wid) for wid in raw_ids if isinstance(wid, str))
        elif tool == "amend_exit":
            # L3 — patch du plan de sortie ouvert. Réutilise _compact_exit_plan
            # (même vocabulaire que propose_order.exit : stop/tp/trail/protect).
            amend = _compact_exit_plan(args)
            if amend:
                decision["amend_exit"] = amend
        else:
            raise ValueError(f"unknown_action_tool:{tool}")

    decision["cancel_watch_ids"] = cancel_ids
    resolve_from_position = decision.pop("_resolve_from_position", False)
    reduce_fraction_val = decision.pop("_reduce_fraction", None)
    parsed = _decision_from_dict(decision, symbol)
    result = replace(
        parsed,
        domain_tools={"tool_rounds": 0, "tool_calls": traces},
        thesis=decision.get("thesis"),
        next_wake_event=next_wake_event,
        risk_pct_target=risk_pct_target_local,
    )
    if resolve_from_position:
        result = replace(result, resolve_from_position=True)
    if reduce_fraction_val is not None:
        result = replace(result, reduce_fraction=reduce_fraction_val)
    return result


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
            if isinstance(element, dict) and "calls" in element:
                by_symbol[sym] = _decision_from_symbol_calls(element, sym)
            elif allow_context_request:
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
