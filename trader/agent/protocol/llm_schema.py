"""Pydantic schemas for raw LLM protocol payloads."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

INLINE_DECISION_FIELDS = frozenset(
    {
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
)
RELATIVE_ORDER_INTENTS = frozenset({"CLOSE", "REDUCE", "FLIP", "SCALE_IN"})
DECISION_ACTIONS = frozenset({"BUY", "SELL", "HOLD"})
OPPORTUNITY_SIDES = frozenset({"long", "short"})


def _coerce_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid number") from exc


def _coerce_optional_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _coerce_float(value, field_name)


def _coerce_optional_dict(value: Any, field_name: str) -> dict | None:
    if value is None:
        return None
    try:
        return dict(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an object") from exc


class _LlmBaseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_default=True)


class LlmDecisionPayload(_LlmBaseModel):
    symbol: Any
    action: Any
    quantity: float
    confidence: float
    rationale: Any
    opportunity_side: str | None = None
    decision_reason_code: Any | None = None
    intent: str | None = None
    exit_plan: dict | None = None
    indicator_watch: dict | None = None
    cancel_watch_ids: list[str] = Field(default_factory=list)
    next_wake_in_minutes: float | None = None
    learning: Any | None = None
    applied_learning_ids: list[str] = Field(default_factory=list)
    exit_update: dict | None = None
    reduce_fraction_internal: Any | None = Field(default=None, alias="_reduce_fraction")

    @field_validator("quantity", "confidence", mode="before")
    @classmethod
    def _float_fields(cls, value: Any, info) -> float:
        return _coerce_float(value, info.field_name)

    @field_validator("next_wake_in_minutes", mode="before")
    @classmethod
    def _optional_float_fields(cls, value: Any, info) -> float | None:
        return _coerce_optional_float(value, info.field_name)

    @field_validator("intent", mode="before")
    @classmethod
    def _upper_optional_intent(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).upper()

    @field_validator("opportunity_side", mode="before")
    @classmethod
    def _opportunity_side(cls, value: Any) -> str | None:
        if value is None:
            return None
        side = str(value).strip().lower()
        if side not in OPPORTUNITY_SIDES:
            raise ValueError("opportunity_side must be long, short, or null")
        return side

    @field_validator("exit_plan", "indicator_watch", "exit_update", mode="before")
    @classmethod
    def _optional_dict_fields(cls, value: Any, info) -> dict | None:
        return _coerce_optional_dict(value, info.field_name)

    @field_validator("cancel_watch_ids", mode="before")
    @classmethod
    def _cancel_watch_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(wid) for wid in value if isinstance(wid, str)]

    @field_validator("applied_learning_ids", mode="before")
    @classmethod
    def _applied_learning_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        selected: list[str] = []
        seen: set[str] = set()
        for raw_id in value:
            if not isinstance(raw_id, str):
                continue
            rule_id = raw_id.strip()
            if not rule_id or rule_id in seen:
                continue
            seen.add(rule_id)
            selected.append(rule_id)
            if len(selected) >= 3:
                break
        return selected

    @model_validator(mode="after")
    def _validate_action_and_relative_quantity(self) -> "LlmDecisionPayload":
        if self.intent in RELATIVE_ORDER_INTENTS:
            has_reduce_fraction = self.reduce_fraction_internal is not None
            if (
                self.quantity < 0.0
                or self.intent in {"FLIP", "SCALE_IN"}
                and self.quantity <= 0.0
                or self.intent == "REDUCE"
                and self.quantity <= 0.0
                and not has_reduce_fraction
            ):
                raise ValueError("order_qty_must_be_positive")
            return self

        action = str(self.action).upper()
        if action not in DECISION_ACTIONS:
            raise ValueError(f"action invalide: {action}")
        self.action = action
        return self

    def to_legacy_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "symbol": self.symbol,
            "action": self.action,
            "quantity": self.quantity,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "opportunity_side": self.opportunity_side,
            "decision_reason_code": self.decision_reason_code,
            "intent": self.intent,
            "exit_plan": self.exit_plan,
            "indicator_watch": self.indicator_watch,
            "cancel_watch_ids": self.cancel_watch_ids,
            "next_wake_in_minutes": self.next_wake_in_minutes,
            "learning": self.learning,
            "applied_learning_ids": self.applied_learning_ids,
            "exit_update": self.exit_update,
        }
        if self.reduce_fraction_internal is not None:
            data["_reduce_fraction"] = self.reduce_fraction_internal
        return data


class LlmSymbolToolCallPayload(_LlmBaseModel):
    id: Any | None = None
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool", mode="before")
    @classmethod
    def _tool_name(cls, value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("tool_name_required")
        return value

    @field_validator("args", mode="before")
    @classmethod
    def _args_object(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("tool_args_must_be_object")
        return value

    def to_legacy_dict(self) -> dict[str, Any]:
        data = {"tool": self.tool, "args": self.args}
        if self.id is not None:
            data["id"] = self.id
        return data


class LlmSymbolCallsPayload(_LlmBaseModel):
    calls: list[LlmSymbolToolCallPayload]
    confidence: float = 0.0
    rationale: str = ""
    opportunity_side: str | None = None
    decision_reason_code: Any | None = None
    applied_learning_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_mixed_inline_decision(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("element must be an object")
        if any(field in value for field in INLINE_DECISION_FIELDS):
            raise ValueError("mixed_inline_decision_and_tools")
        return value

    @field_validator("calls", mode="before")
    @classmethod
    def _calls_list(cls, value: Any) -> list[Any]:
        if not isinstance(value, list):
            raise ValueError("calls_must_be_list")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        return _coerce_float(value or 0.0, "confidence")

    @field_validator("rationale", mode="before")
    @classmethod
    def _rationale(cls, value: Any) -> str:
        return str(value or "")

    @field_validator("opportunity_side", mode="before")
    @classmethod
    def _opportunity_side(cls, value: Any) -> str | None:
        return LlmDecisionPayload._opportunity_side(value)

    @field_validator("applied_learning_ids", mode="before")
    @classmethod
    def _applied_learning_ids(cls, value: Any) -> list[str]:
        return LlmDecisionPayload._applied_learning_ids(value)

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "calls": [call.to_legacy_dict() for call in self.calls],
            "confidence": self.confidence,
            "rationale": self.rationale,
            "opportunity_side": self.opportunity_side,
            "decision_reason_code": self.decision_reason_code,
            "applied_learning_ids": self.applied_learning_ids,
        }


class LlmIndicatorRequestPayload(_LlmBaseModel):
    symbol: Any | None = None
    indicators: list[Any] = Field(default_factory=list)
    timeframe: Any = "1h"
    lookback: Any | None = None
    window: int = 48
    as_of: Any = "latest"

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("indicator_request_must_be_object")
        data = dict(value)
        indicators = data.get("indicators") or data.get("names") or data.get("indicator") or []
        if isinstance(indicators, str):
            indicators = [indicators]
        data["indicators"] = indicators
        data["timeframe"] = data.get("timeframe") or data.get("interval") or "1h"
        data["window"] = data.get("window") or 48
        data["as_of"] = data.get("as_of") or "latest"
        return data

    @field_validator("window", mode="before")
    @classmethod
    def _window(cls, value: Any) -> int:
        try:
            return int(value or 48)
        except (TypeError, ValueError) as exc:
            raise ValueError("window must be an integer") from exc


class LlmContextRequestPayload(_LlmBaseModel):
    symbol: Any | None = None
    action: Any | None = None
    needs_context: Any | None = None
    rationale: str = ""
    requests: list[Any] = Field(default_factory=list)
    next_wake_in_minutes: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_request_alias(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("context_request_must_be_object")
        data = dict(value)
        raw_requests = data.get("requests") or data.get("indicator_requests") or []
        data["requests"] = [] if isinstance(raw_requests, dict) else raw_requests
        return data

    @field_validator("rationale", mode="before")
    @classmethod
    def _context_rationale(cls, value: Any) -> str:
        return str(value or "")

    @field_validator("requests", mode="before")
    @classmethod
    def _requests_list(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("requests must be a list")
        return value

    @field_validator("next_wake_in_minutes", mode="before")
    @classmethod
    def _next_wake(cls, value: Any, info) -> float | None:
        return _coerce_optional_float(value, info.field_name)

    @classmethod
    def is_context_request(cls, value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        action = str(value.get("action", "")).upper()
        return action in {"REQUEST_CONTEXT", "NEEDS_CONTEXT"} or value.get("needs_context") is True

    def indicator_payloads(self) -> list[LlmIndicatorRequestPayload]:
        payloads: list[LlmIndicatorRequestPayload] = []
        for item in self.requests:
            if not isinstance(item, dict):
                continue
            payloads.append(LlmIndicatorRequestPayload.model_validate(item))
        return payloads


class LlmBatchPayload(_LlmBaseModel):
    decisions: list[Any]

    @field_validator("decisions", mode="before")
    @classmethod
    def _decisions_list(cls, value: Any) -> list[Any]:
        if not isinstance(value, list):
            raise ValueError("decisions must be a list")
        return value


class LlmBatchToolCallsPayload(_LlmBaseModel):
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _filter_raw_tool_calls(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]
