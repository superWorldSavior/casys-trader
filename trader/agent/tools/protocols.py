"""Provider protocols injected into read-only agent tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from trader.application.execute.trade_plan_evaluation import (
        TradePlanCandidate,
        TradePlanEvaluation,
    )

ToolPayload = dict[str, Any]


class IndicatorResolver(Protocol):
    def __call__(self, requests: list[Any]) -> ToolPayload: ...


class LearningsRecallProvider(Protocol):
    def __call__(self, query: ToolPayload) -> ToolPayload: ...


class OpenPlansProvider(Protocol):
    def __call__(self) -> list: ...


class OpenPlansAsOfProvider(Protocol):
    def __call__(self) -> str | None: ...


class TradePlanEvaluatorPort(Protocol):
    def evaluate(
        self,
        candidate: "TradePlanCandidate",
    ) -> "TradePlanEvaluation": ...


class TradePlanEvaluatorProvider(Protocol):
    def __call__(self, symbol: str) -> TradePlanEvaluatorPort | None: ...


__all__ = [
    "IndicatorResolver",
    "LearningsRecallProvider",
    "OpenPlansAsOfProvider",
    "OpenPlansProvider",
    "TradePlanEvaluatorPort",
    "TradePlanEvaluatorProvider",
]
