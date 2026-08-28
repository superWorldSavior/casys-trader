"""Pure LLM result values, backend port and fallback routing policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LlmExecutionCapability:
    """What the receiving backend or session can actually execute.

    Prompt construction must advertise only this contract. It must not infer
    tools from process-wide flags such as ``CASYS_AGENT_EXEC``.
    """

    caged_native_python: bool = False

    @classmethod
    def none(cls) -> LlmExecutionCapability:
        return cls()

    @classmethod
    def shared(cls, capabilities: list[LlmExecutionCapability]) -> LlmExecutionCapability:
        if not capabilities:
            return cls()
        return cls(caged_native_python=all(item.caged_native_python for item in capabilities))


def execution_capability_of(target: object) -> LlmExecutionCapability:
    method = getattr(target, "execution_capability", None)
    if callable(method):
        capability = method()
        if isinstance(capability, LlmExecutionCapability):
            return capability
    return LlmExecutionCapability.none()


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


@runtime_checkable
class LlmBackend(Protocol):
    provider: str
    model: str

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure: ...


@runtime_checkable
class LlmSession(Protocol):
    """A provider-owned decision conversation which can receive many prompts."""

    def send(self, prompt: str, *, timeout_s: int, call_ctx: dict | None = None) -> LlmCompletion | LlmFailure: ...

    def close(self) -> None: ...


@runtime_checkable
class SessionLlmBackend(LlmBackend, Protocol):
    """Backend port required by the queued decision/tool round."""

    def open_session(self, name: str, *, timeout_s: int) -> LlmSession | LlmFailure: ...


class LlmRouter:
    """Try ordered backends and preserve the first retryable fallback reason."""

    def __init__(self, backends: list[LlmBackend]) -> None:
        self.backends = backends

    def execution_capability(self) -> LlmExecutionCapability:
        """Conservative intersection: one-shot prompts reuse one text for every tier."""

        return LlmExecutionCapability.shared([execution_capability_of(backend) for backend in self.backends])

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
                fallback_reason = result.fallback_reason or f"{result.provider}:{result.code}"
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
    "LlmExecutionCapability",
    "LlmFailure",
    "LlmRouter",
    "LlmSession",
    "SessionLlmBackend",
    "execution_capability_of",
]
