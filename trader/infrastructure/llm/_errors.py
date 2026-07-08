"""Shared provider-error classification for LLM infrastructure adapters."""

from __future__ import annotations


def _looks_retryable_provider_error(text: str) -> bool:
    """Vrai uniquement quand le fournisseur refuse de servir pour rate-limit/quota."""
    lowered = text.lower()
    needles = (
        "429",
        "rate limit",
        "rate_limit",
        "quota",
        "credit",
        "credits",
        "insufficient_quota",
    )
    return any(needle in lowered for needle in needles)


def _failure_code_from_text(text: str) -> str:
    lowered = text.lower()
    if "429" in lowered or "rate limit" in lowered or "rate_limit" in lowered:
        return "rate_limited"
    if "quota" in lowered or "credit" in lowered or "insufficient_quota" in lowered:
        return "quota_exceeded"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    return "provider_error"


__all__ = [
    "_failure_code_from_text",
    "_looks_retryable_provider_error",
]
