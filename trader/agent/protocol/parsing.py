"""Parsing helpers for agent JSON responses."""

from __future__ import annotations

import json
from dataclasses import replace

from pydantic import ValidationError

from trader.domain import decision_reason
from trader.agent.protocol.llm_schema import (
    INLINE_DECISION_FIELDS,
    RELATIVE_ORDER_INTENTS,
    LlmBatchPayload,
    LlmBatchToolCallsPayload,
    LlmContextRequestPayload,
    LlmDecisionPayload,
    LlmSymbolCallsPayload,
)
from trader.agent.protocol.types import (
    BatchToolCallRequest,
    ContextResearchRequest,
    Decision,
    IndicatorRequest,
)
from trader.agent.protocol.json_utils import extract_json_object
from trader.agent.protocol.strategy_language import compile_strategy_call
from trader.domain.strategy_language import (
    normalize_trade_thesis as _canonical_trade_thesis,
)

_DECISION_KEYS = {"symbol", "action", "quantity", "confidence", "rationale", "decision_reason_code"}
_INLINE_DECISION_FIELDS = set(INLINE_DECISION_FIELDS)
MAX_LEARNING_CHARS = 1000  # borne la note pour ne pas faire exploser le prompt/store
_RELATIVE_ORDER_INTENTS = RELATIVE_ORDER_INTENTS


def _normalize_thesis(value: object) -> dict | None:
    """Valide et borne le thesis tag {setup, horizon, invalidation}.

    Fail-safe : tout thesis malformé, partiel ou hors-contrat retourne None
    sans faire tomber la décision. Règles :
    - value doit être un dict
    - setup et invalidation : str non vide, tronqués à MAX_THESIS_FIELD_CHARS
    - horizon : str dans {intraday, swing, position} (case-insensitive, normalisé lowercase)
    - un seul champ manquant → None
    """
    return _canonical_trade_thesis(value)


def normalize_trade_thesis(value: object) -> dict | None:
    """Public canonicalizer shared by evaluation tools and final parsing."""

    return _normalize_thesis(value)


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
    payload = extract_json_object(text, last=False)
    if payload is None:
        raise ValueError("aucun objet JSON trouvé")
    return payload


def _format_validation_error(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors(include_url=False):
        loc = ".".join(str(part) for part in item.get("loc", ())) or "payload"
        msg = str(item.get("msg") or "invalid value")
        parts.append(f"{loc}: {msg}")
    return "; ".join(parts) or str(error)


def _format_parse_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return _format_validation_error(error)
    return str(error)


def _invalid_payload_hold(symbol: str, error: Exception) -> Decision:
    return _hold_parse_error(
        symbol,
        f"parse_error:invalid_payload: {_format_parse_error(error)}",
        "parse_error:invalid_payload",
    )


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


def _decision_from_dict(data: dict | LlmDecisionPayload, symbol: str) -> Decision:
    payload = data if isinstance(data, LlmDecisionPayload) else LlmDecisionPayload.model_validate(data)
    data = payload.to_legacy_dict()
    missing = _DECISION_KEYS - data.keys()
    blocking_missing = missing - {"decision_reason_code"}
    if blocking_missing:
        raise ValueError(f"clés manquantes: {blocking_missing}")
    intent = _optional_upper_str(data, "intent")
    quantity = float(data["quantity"])
    resolve_from_position = False
    normalizations: list[dict] = []
    if intent in _RELATIVE_ORDER_INTENTS:
        has_reduce_fraction = data.get("_reduce_fraction") is not None
        if (
            quantity < 0.0
            or intent in {"FLIP", "SCALE_IN"}
            and quantity <= 0.0
            or intent == "REDUCE"
            and quantity <= 0.0
            and not has_reduce_fraction
        ):
            raise ValueError("order_qty_must_be_positive")
        action = "HOLD"
        resolve_from_position = True
        if intent == "CLOSE":
            quantity = 0.0
        normalizations.append(
            {
                "code": "relative_intent_position_resolved",
                "ignored_fields": ["action"],
            }
        )
    else:
        action = str(data["action"]).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            raise ValueError(f"action invalide: {action}")
    return Decision(
        symbol=str(data["symbol"]),
        action=action,  # type: ignore[arg-type]
        quantity=quantity,
        confidence=float(data["confidence"]),
        rationale=str(data["rationale"]),
        opportunity_side=data.get("opportunity_side"),
        next_wake_in_minutes=_optional_float(data, "next_wake_in_minutes"),
        intent=intent,  # type: ignore[arg-type]
        exit_plan=_optional_dict(data, "exit_plan"),
        indicator_watch=_optional_dict(data, "indicator_watch"),
        cancel_watch_ids=_cancel_watch_ids(data),
        learning=_normalize_learning(data.get("learning")),
        applied_learning_ids=list(data.get("applied_learning_ids") or []),
        decision_reason_code=_decision_reason_code(data),
        exit_update=_optional_dict(data, "exit_update"),
        resolve_from_position=resolve_from_position,
        domain_tools=({"normalizations": normalizations} if normalizations else None),
        trade_evaluation_id=(
            str(data["trade_evaluation_id"])
            if isinstance(data.get("trade_evaluation_id"), str)
            and data["trade_evaluation_id"]
            else None
        ),
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


def compact_trade_exit_plan(raw: object) -> dict | None:
    """Public canonicalizer shared by evaluation tools and final parsing."""

    return _compact_exit_plan(raw)


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


def _decision_from_symbol_calls(data: dict | LlmSymbolCallsPayload, symbol: str) -> Decision:
    payload = data if isinstance(data, LlmSymbolCallsPayload) else LlmSymbolCallsPayload.model_validate(data)
    data = payload.to_legacy_dict()
    calls = data.get("calls")

    decision: dict = {
        "symbol": symbol,
        "action": "HOLD",
        "quantity": 0.0,
        "confidence": float(data.get("confidence") or 0.0),
        "rationale": str(data.get("rationale") or ""),
        "opportunity_side": data.get("opportunity_side"),
        "intent": "HOLD",
        "decision_reason_code": _decision_reason_code(data),
        "applied_learning_ids": list(data.get("applied_learning_ids") or []),
    }
    traces: list[dict] = []
    cancel_ids: list[str] = []
    next_wake_event: str | None = None
    # L1 — sizing en risque : extrait de l'action d'entrée.
    risk_pct_target_local: float | None = None
    # L5-unify — set_next_wake{when} : coexistence avec propose_indicator_watch
    wake_when_used: bool = False
    propose_indicator_watch_used: bool = False
    position_order_used: bool = False
    exit_rule_used: bool = False
    public_position_tool: str | None = None
    public_exit_tool_used: bool = False

    for index, raw in enumerate(calls):
        if not isinstance(raw, dict):
            raise ValueError("tool_call_must_be_object")
        tool = raw.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ValueError("tool_name_required")
        args = _tool_args(raw)
        call_id = _tool_call_id(symbol, index, raw)
        traces.append({"id": call_id, "tool": tool, "args": args, "outcome": "ok", "detail": {}})
        compiled = compile_strategy_call(tool, args)
        primitive = compiled.primitive
        args = dict(compiled.args)
        if primitive == "position_order":
            position_order_used = True
            public_position_tool = compiled.public_tool
            intent = str(args.get("intent") or "").upper()
            if intent not in {"OPEN_LONG", "OPEN_SHORT", "REDUCE", "CLOSE", "FLIP", "SCALE_IN"}:
                raise ValueError("order_intent_invalid")
            risk_pct_raw = args.get("risk_pct")
            if risk_pct_raw is not None and intent not in {"OPEN_LONG", "OPEN_SHORT"}:
                traces[-1]["detail"].setdefault("ignored_fields", []).append(
                    {"field": "risk_pct", "reason": "risk_pct_only_used_for_open_intents"}
                )

            strategy_entry_position_aware = compiled.position_aware and intent in {"OPEN_LONG", "OPEN_SHORT"}
            needs_position_resolve = False
            reduce_fraction: float | None = None
            if intent in _RELATIVE_ORDER_INTENTS or compiled.position_aware:
                ignored_side_fields = [field for field in ("side", "action") if args.get(field) is not None]
                if ignored_side_fields and not strategy_entry_position_aware:
                    traces[-1]["detail"].setdefault("ignored_fields", []).append(
                        {
                            "fields": ignored_side_fields,
                            "reason": "relative_intent_side_derived_from_position",
                        }
                    )
                needs_position_resolve = True
                action = _action_for_order_tool(args) if strategy_entry_position_aware else "HOLD"
                if intent == "REDUCE":
                    frac = args.get("fraction")
                    if frac is not None:
                        reduce_fraction = float(frac)
                        if not (0.0 < reduce_fraction <= 1.0):
                            raise ValueError("reduce_fraction_out_of_range")
            else:
                action = _action_for_order_tool(args)

            qty_raw = args.get("qty") if args.get("qty") is not None else args.get("quantity")
            if qty_raw is not None and float(qty_raw) <= 0.0:
                raise ValueError("order_qty_must_be_positive")
            if needs_position_resolve:
                if strategy_entry_position_aware:
                    if qty_raw is not None:
                        qty = float(qty_raw)
                    elif risk_pct_raw is not None:
                        qty = 0.0
                        risk_pct_target_local = float(risk_pct_raw)
                    else:
                        raise ValueError("order_qty_required")
                elif intent == "CLOSE":
                    qty = 0.0  # Dérivé de |position| dans le daemon
                elif intent == "REDUCE":
                    qty = float(qty_raw) if qty_raw is not None else 0.0
                else:  # FLIP / SCALE_IN : qty requise (jambe cible ou renforcement)
                    if qty_raw is None:
                        raise ValueError("order_qty_required")
                    qty = float(qty_raw)
            else:
                # L1 — risk_pct est une alternative à qty pour OPEN_LONG/OPEN_SHORT.
                # Explicit qty > risk_pct (Explicit Over Implicit).
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
            evaluation_id = args.get(
                "trade_evaluation_id",
                args.get("evaluation_id"),
            )
            decision["trade_evaluation_id"] = (
                str(evaluation_id)
                if isinstance(evaluation_id, str) and evaluation_id
                else None
            )
            if needs_position_resolve:
                decision["_resolve_from_position"] = True
                if reduce_fraction is not None:
                    decision["_reduce_fraction"] = reduce_fraction
        elif primitive == "set_next_wake":
            on_event = args.get("on")
            minutes = args.get("minutes")
            when = args.get("when")
            if when is not None:
                # Réveil-sur-indicateur : {when: <condition>} → indicator_watch{WAKE}.
                # Réutilise le mécanisme propose_indicator_watch{WAKE} sans le dupliquer :
                # on assemble le dict brut que build_indicator_watch (daemon) consomme.
                # Validation indicateur/opérateur : déléguée à build_indicator_watch (atomicité).
                if not isinstance(when, dict):
                    raise ValueError("wake_when_must_be_object")
                watch_raw: dict = {"on_trigger": "WAKE", "logic": "all", "conditions": [when]}
                if args.get("ttl_minutes") is not None:
                    watch_raw["ttl_minutes"] = args["ttl_minutes"]
                decision["indicator_watch"] = watch_raw
                wake_when_used = True
            elif on_event is not None:
                # Réveil événementiel : stocké pour résolution au daemon.
                # Un on: inconnu est accepté ici ; le daemon tombera en fail-safe.
                next_wake_event = str(on_event)
            elif minutes is not None:
                decision["next_wake_in_minutes"] = float(minutes)
            else:
                raise ValueError("wake_minutes_required")
        elif primitive == "record_learning":
            decision["learning"] = _normalize_learning(args.get("note"))
        elif primitive == "propose_indicator_watch":
            watch = args.get("watch")
            decision["indicator_watch"] = dict(watch) if isinstance(watch, dict) else dict(args)
            propose_indicator_watch_used = True
        elif primitive == "cancel_watch":
            raw_ids = args.get("ids") or args.get("watch_ids")
            if raw_ids is None and isinstance(args.get("id"), str):
                raw_ids = [args["id"]]
            if not isinstance(raw_ids, list):
                raise ValueError("cancel_watch_ids_required")
            cancel_ids.extend(str(wid) for wid in raw_ids if isinstance(wid, str))
        elif primitive == "exit_rule":
            exit_rule_used = True
            if compiled.public_tool == "strategy_exit":
                public_exit_tool_used = True
            # L3 — patch du plan de sortie ouvert. Réutilise _compact_exit_plan
            # (même vocabulaire compact que strategy_entry.exit).
            update = _compact_exit_plan(args)
            if update:
                decision["exit_update"] = update

    decision["cancel_watch_ids"] = cancel_ids
    # Coexistence : set_next_wake{when} et propose_indicator_watch écrivent tous deux
    # indicator_watch → ambiguïté. On rejette explicitement plutôt que de silencieusement
    # laisser le dernier gagner (AX : Explicit Over Implicit).
    if wake_when_used and propose_indicator_watch_used:
        raise ValueError("wake_when_conflicts_with_propose_indicator_watch")
    if position_order_used and exit_rule_used:
        if public_exit_tool_used and public_position_tool is not None:
            raise ValueError(f"strategy_exit_conflicts_with_{public_position_tool}")
        raise ValueError("strategy_exit_conflicts_with_position_order")
    indicator_watch = decision.get("indicator_watch")
    if isinstance(indicator_watch, dict) and "rationale" not in indicator_watch:
        # La watch survivra à la session LLM puis reviendra comme trigger. Garder
        # l'intention formulée au niveau décision empêche ce rappel de repartir
        # sans le pourquoi du seuil, tout en respectant une rationale watch explicite.
        indicator_watch["rationale"] = decision["rationale"]
    resolve_from_position = decision.pop("_resolve_from_position", False)
    reduce_fraction_val = decision.get("_reduce_fraction")
    parsed = _decision_from_dict(decision, symbol)
    decision.pop("_reduce_fraction", None)
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
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError) as e:
        return _hold_parse_error(symbol, f"parse_error:invalid_json: {e}", "parse_error:invalid_json")
    try:
        if "calls" in data:
            return _decision_from_symbol_calls(LlmSymbolCallsPayload.model_validate(data), symbol)
        return _decision_from_dict(LlmDecisionPayload.model_validate(data), symbol)
    except (ValidationError, ValueError, TypeError) as e:
        return _invalid_payload_hold(symbol, e)


def _indicator_requests_from(data: dict | LlmContextRequestPayload, symbol: str) -> list[IndicatorRequest]:
    payload = data if isinstance(data, LlmContextRequestPayload) else LlmContextRequestPayload.model_validate(data)
    requests: list[IndicatorRequest] = []
    for item in payload.indicator_payloads():
        requests.append(
            IndicatorRequest(
                symbol=str(item.symbol or symbol),
                indicators=[str(name) for name in item.indicators],
                timeframe=str(item.timeframe or "1h"),
                lookback=(None if item.lookback is None else str(item.lookback)),
                window=int(item.window or 48),
                as_of=str(item.as_of or "latest"),
            )
        )
    return requests


def _response_from_dict(data: dict, symbol: str) -> Decision | ContextResearchRequest:
    if not isinstance(data, dict):
        raise ValueError("élément non-objet")
    if "calls" in data:
        return _decision_from_symbol_calls(LlmSymbolCallsPayload.model_validate(data), symbol)
    if LlmContextRequestPayload.is_context_request(data):
        payload = LlmContextRequestPayload.model_validate(data)
        return ContextResearchRequest(
            symbol=str(payload.symbol or symbol),
            rationale=payload.rationale,
            requests=_indicator_requests_from(payload, symbol),
            next_wake_in_minutes=payload.next_wake_in_minutes,
        )
    return _decision_from_dict(LlmDecisionPayload.model_validate(data), symbol)


def parse_decision_or_context_request(raw_text: str, symbol: str) -> Decision | ContextResearchRequest:
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError) as e:
        return _hold_parse_error(symbol, f"parse_error:invalid_json: {e}", "parse_error:invalid_json")
    try:
        return _response_from_dict(data, symbol)
    except (ValidationError, ValueError, TypeError) as e:
        return _invalid_payload_hold(symbol, e)


def _hold_parse_error(symbol: str, reason: str, llm_error: str) -> Decision:
    """HOLD synthétique d'erreur de parsing — llm_error explicite pour decide_one.

    Codes llm_error par cas :
      parse_error:invalid_json       — JSON global invalide ou absent
      parse_error:invalid_payload    — payload single invalide après extraction
      parse_error:bad_decisions_field — champ decisions présent mais non-list
      tool_loop                      — tool_calls sans decisions (tour final)
      parse_error:corrupt_element    — élément decisions malformé (parse individuel)
      parse_error:missing_symbol     — symbole absent de la réponse batch
    """
    return replace(Decision.hold(symbol, reason), llm_error=llm_error)


def _parse_batch_data(
    data: dict, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest]:
    """Corps de parse_batch sur un dict déjà extrait. Isolation per-élément."""
    by_symbol: dict[str, Decision | ContextResearchRequest] = {}
    try:
        batch = LlmBatchPayload.model_validate(data)
    except ValidationError:
        # tool_calls SANS clé decisions au tour final → raison explicite (défense
        # en profondeur). Mais `decisions` présent-et-malformé reste un
        # batch_bad_output : ne pas le masquer en tool_loop_blocked.
        raw_decisions = data.get("decisions") if isinstance(data, dict) else None
        raw_calls = data.get("tool_calls") if isinstance(data, dict) else None
        if raw_decisions is None and isinstance(raw_calls, list) and any(isinstance(c, dict) for c in raw_calls):
            return {sym: _hold_parse_error(sym, "tool_loop_blocked", "tool_loop") for sym in symbols}
        return {sym: _hold_parse_error(sym, "batch_bad_output", "parse_error:bad_decisions_field") for sym in symbols}
    decisions = batch.decisions

    requested = set(symbols)
    for element in decisions:
        sym = str(element.get("symbol")) if isinstance(element, dict) else None
        if sym is None or sym not in requested:
            continue  # symbole hors périmètre ou élément non-objet -> ignoré
        try:
            if isinstance(element, dict) and "calls" in element:
                by_symbol[sym] = _decision_from_symbol_calls(LlmSymbolCallsPayload.model_validate(element), sym)
            elif allow_context_request:
                by_symbol[sym] = _response_from_dict(element, sym)
            else:
                by_symbol[sym] = _decision_from_dict(LlmDecisionPayload.model_validate(element), sym)
        except Exception as e:  # noqa: BLE001 - isolation per-élément
            by_symbol[sym] = _hold_parse_error(
                sym,
                f"batch_bad_output: {_format_parse_error(e)}",
                "parse_error:corrupt_element",
            )

    for sym in symbols:
        by_symbol.setdefault(sym, _hold_parse_error(sym, "missing_in_batch", "parse_error:missing_symbol"))
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
        return {sym: _hold_parse_error(sym, "batch_bad_output", "parse_error:invalid_json") for sym in symbols}
    return _parse_batch_data(data, symbols, allow_context_request=allow_context_request)


def parse_batch_or_tool_calls(
    raw_text: str, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest] | BatchToolCallRequest:
    """Réponse batch OU tournée d'outils. tool_calls non vide prime ; toute
    malformation retombe sur le chemin décisions (fail-safe HOLD)."""
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError):
        return {sym: _hold_parse_error(sym, "batch_bad_output", "parse_error:invalid_json") for sym in symbols}
    try:
        calls = LlmBatchToolCallsPayload.model_validate(data).tool_calls
    except ValidationError:
        calls = []
    if calls:
        return BatchToolCallRequest(calls=calls)
    return _parse_batch_data(data, symbols, allow_context_request=allow_context_request)
