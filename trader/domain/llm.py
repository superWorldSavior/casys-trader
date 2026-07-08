"""Pure LLM result value objects."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LlmCompletion:
    provider: str
    model: str
    text: str
    fallback_reason: str | None = None


@dataclass(frozen=True)
class LlmFailure:
    provider: str
    model: str
    code: str
    message: str
    retryable: bool
    fallback_reason: str | None = None


__all__ = [
    "LlmCompletion",
    "LlmFailure",
]
