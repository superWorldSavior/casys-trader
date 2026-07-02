"""Agent protocol contracts, prompt builders, and response parsers.

The package root keeps DTO imports cheap. Prompt builders and parsers are
loaded lazily because they pull in market/planning/reporting dependencies.
"""

from __future__ import annotations

import importlib
from typing import Any

from trader.agent_protocol.types import (
    Action,
    BatchToolCallRequest,
    ContextResearchRequest,
    Decision,
    IndicatorRequest,
    Intent,
)

_LAZY_EXPORTS = {
    "build_batch_prompt": "trader.agent_protocol.prompts",
    "build_prompt": "trader.agent_protocol.prompts",
    "parse_batch": "trader.agent_protocol.parsing",
    "parse_batch_or_tool_calls": "trader.agent_protocol.parsing",
    "parse_decision": "trader.agent_protocol.parsing",
    "parse_decision_or_context_request": "trader.agent_protocol.parsing",
}

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


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value
