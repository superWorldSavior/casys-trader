"""Provider protocols injected into read-only agent tools."""

from __future__ import annotations

from typing import Any, Protocol

ToolPayload = dict[str, Any]


class IndicatorResolver(Protocol):
    def __call__(self, requests: list[Any]) -> ToolPayload: ...


class LearningsRecallProvider(Protocol):
    def __call__(self, query: ToolPayload) -> ToolPayload: ...


class OpenPlansProvider(Protocol):
    def __call__(self) -> list: ...


class OpenPlansAsOfProvider(Protocol):
    def __call__(self) -> str | None: ...


__all__ = [
    "IndicatorResolver",
    "LearningsRecallProvider",
    "OpenPlansAsOfProvider",
    "OpenPlansProvider",
]
