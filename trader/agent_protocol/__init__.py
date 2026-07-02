"""Agent protocol contracts, prompt builders, and response parsers."""

from trader.agent_protocol.parsing import (
    parse_batch,
    parse_batch_or_tool_calls,
    parse_decision,
    parse_decision_or_context_request,
)
from trader.agent_protocol.prompts import build_batch_prompt, build_prompt
from trader.agent_protocol.types import (
    Action,
    BatchToolCallRequest,
    ContextResearchRequest,
    Decision,
    IndicatorRequest,
    Intent,
)

__all__ = [
    "Action",
    "BatchToolCallRequest",
    "ContextResearchRequest",
    "Decision",
    "IndicatorRequest",
    "Intent",
    "build_batch_prompt",
    "build_prompt",
    "parse_batch",
    "parse_batch_or_tool_calls",
    "parse_decision",
    "parse_decision_or_context_request",
]
