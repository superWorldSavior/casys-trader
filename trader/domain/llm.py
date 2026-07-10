"""Pure LLM result values, backend port and fallback routing policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol


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


class LlmBackend(Protocol):
    provider: str
    model: str

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        ...


class LlmRouter:
    """Try ordered backends and preserve the first retryable fallback reason."""

    def __init__(self, backends: list[LlmBackend]) -> None:
        self.backends = backends

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        fallback_reason: str | None = None
        last_failure: LlmFailure | None = None
        for index, backend in enumerate(self.backends):
            result = backend.complete(prompt, timeout_s=timeout_s)
            if isinstance(result, LlmCompletion):
                if fallback_reason and result.fallback_reason is None:
                    return replace(result, fallback_reason=fallback_reason)
                return result

            last_failure = result
            has_next = index < len(self.backends) - 1
            if has_next and result.retryable:
                fallback_reason = (
                    result.fallback_reason
                    or f"{result.provider}:{result.code}"
                )
                continue
            if fallback_reason and result.fallback_reason is None:
                return replace(result, fallback_reason=fallback_reason)
            return result

        return last_failure or LlmFailure(
            provider="none",
            model="none",
            code="no_backend",
            message="aucun backend LLM configuré",
            retryable=False,
        )


__all__ = [
    "LlmBackend",
    "LlmCompletion",
    "LlmFailure",
    "LlmRouter",
]
