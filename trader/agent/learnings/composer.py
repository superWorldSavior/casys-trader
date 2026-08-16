"""Adapter: prompt + LLM + JSON recovery behind LearningComposer."""

from __future__ import annotations

import json

from trader.agent import llm
from trader.agent.learnings.consolidation_prompt import build_consolidation_prompt
from trader.domain.llm import LlmFailure

__all__ = ["LlmLearningComposer"]


def _failure_payload(
    code: str,
    message: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    output: str | None = None,
) -> dict:
    payload = {"error_code": code, "error_message": message[:500]}
    if provider is not None:
        payload["provider"] = provider
    if model is not None:
        payload["model"] = model
    if output is not None:
        payload["output_preview"] = output[:500]
        payload["output_tail"] = output[-500:]
        payload["output_length"] = len(output)
    return payload


def _failure_from_llm(completion: LlmFailure) -> dict:
    return _failure_payload(
        completion.code,
        completion.message,
        provider=completion.provider,
        model=completion.model,
    )


def _looks_like_consolidated_payload(payload: object) -> bool:
    return isinstance(payload, dict) and "global" in payload


def _closing_suffix_for_truncated_json(text: str) -> str | None:
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack.pop() != pairs[char]:
                return None
    if in_string or not stack:
        return None
    return "".join("}" if char == "{" else "]" for char in reversed(stack))


def _repair_truncated_consolidated_payload(text: str) -> dict | None:
    suffix = _closing_suffix_for_truncated_json(text)
    if suffix is None:
        return None
    try:
        payload = json.loads(text + suffix)
    except json.JSONDecodeError:
        return None
    if _looks_like_consolidated_payload(payload):
        return payload
    return None


def _recover_embedded_payload(
    text: str,
    first_error: json.JSONDecodeError,
) -> tuple[dict | None, json.JSONDecodeError | None]:
    decoder = json.JSONDecoder()
    candidate: dict | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            payload = _repair_truncated_consolidated_payload(text[index:])
        if _looks_like_consolidated_payload(payload):
            candidate = payload
    if candidate is not None:
        return candidate, None
    return None, first_error


def parse_consolidated_json(text: str) -> tuple[dict | None, json.JSONDecodeError | None]:
    """Recover the last consolidated JSON object from noisy acpx stdout."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return _recover_embedded_payload(text, exc)
    if isinstance(payload, dict):
        return payload, None
    return None, None


class LlmLearningComposer:
    """Outbound LearningComposer: prompt, one completion, JSON parse."""

    def __init__(self, completer: llm.LlmRouter) -> None:
        self._completer = completer

    def compose(
        self,
        current: dict,
        candidates: list[dict],
        *,
        attribution: dict | None = None,
        meta_performance: dict | None = None,
        timeout_s: int,
        max_attempts: int = 3,
    ) -> tuple[dict | None, dict | None]:
        del max_attempts
        prompt = build_consolidation_prompt(
            current,
            candidates,
            attribution=attribution,
            meta_performance=meta_performance,
        )
        completion = self._completer.complete(prompt, timeout_s=timeout_s)
        if isinstance(completion, LlmFailure):
            return None, {**_failure_from_llm(completion), "retryable": completion.retryable}
        payload, parse_error = parse_consolidated_json(completion.text)
        if payload is not None:
            return payload, None
        if parse_error is not None:
            code, message = "invalid_json", str(parse_error)
        else:
            code, message = "invalid_payload", "consolidateur returned non-object payload"
        return None, {
            **_failure_payload(
                code,
                message,
                provider=completion.provider,
                model=completion.model,
                output=completion.text,
            ),
            "retryable": False,
        }
