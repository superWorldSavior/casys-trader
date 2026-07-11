"""Local decision-planner contract shared by decide application services."""

from __future__ import annotations

from typing import Protocol, TypeAlias

from trader.domain.decisions import BatchToolCallRequest, ContextResearchRequest, Decision

BatchPlannerResponse: TypeAlias = (
    dict[str, Decision | ContextResearchRequest] | BatchToolCallRequest
)


class DecisionBatchPlanner(Protocol):
    """Produce structured decisions for one bounded symbol batch.

    Prompt construction, parsing, tools and transport stay behind this narrow
    application-facing contract. The runtime injects the agent implementation.
    """

    def decide_batch(
        self,
        *,
        symbols: list[str],
        mandate: str,
        memory: str,
        shared_context: dict,
        per_symbol: dict[str, dict],
        allow_context_request: bool,
        allow_tool_calls: bool,
        use_symbol_calls_contract: bool,
        timeout_s: int,
        **kwargs: object,
    ) -> BatchPlannerResponse: ...


__all__ = ["BatchPlannerResponse", "DecisionBatchPlanner"]
