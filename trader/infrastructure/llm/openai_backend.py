"""OpenAI-compatible HTTP transport for the LLM port."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from trader.domain.llm import LlmCompletion, LlmFailure
from trader.infrastructure.llm._errors import _failure_code_from_text, _looks_retryable_provider_error


class OpenAIHttpError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(body)
        self.status = status
        self.body = body


def _post_json(url: str, payload: dict, headers: dict, timeout_s: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OpenAIHttpError(exc.code, body) from exc
    except urllib.error.URLError as exc:
        raise OpenAIHttpError(0, str(exc)) from exc


def _http_error_from_exception(exc: Exception) -> OpenAIHttpError | None:
    if isinstance(exc, OpenAIHttpError):
        return exc
    try:
        data = json.loads(str(exc))
    except json.JSONDecodeError:
        return None
    status = data.get("status")
    if isinstance(status, int):
        return OpenAIHttpError(status, str(data.get("body") or data))
    return None


def _classify_openai_error(exc: Exception) -> tuple[str, bool, str]:
    http_error = _http_error_from_exception(exc)
    if http_error is None:
        message = str(exc)
        return _failure_code_from_text(message), _looks_retryable_provider_error(message), message

    lowered_body = http_error.body.lower()
    if "requires a subscription" in lowered_body or "upgrade for access" in lowered_body:
        return "subscription_required", False, http_error.body
    if http_error.status == 429:
        return "rate_limited", True, http_error.body
    if http_error.status in {408, 500, 502, 503, 504} or http_error.status == 0:
        return "provider_error", False, http_error.body
    if http_error.status in {401, 403}:
        return "auth_failed", False, http_error.body
    if http_error.status == 404:
        return "model_not_found", False, http_error.body
    return "request_failed", False, http_error.body


@dataclass(frozen=True)
class OpenAICompatibleBackend:
    provider: str
    api_key: str
    base_url: str
    model: str
    post_json: Callable[[str, dict, dict, int], dict] = _post_json

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self.post_json(url, payload, headers, timeout_s)
            content = response["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - frontière fournisseur
            code, retryable, message = _classify_openai_error(exc)
            return LlmFailure(
                provider=self.provider,
                model=self.model,
                code=code,
                message=message[:500],
                retryable=retryable,
            )
        return LlmCompletion(provider=self.provider, model=self.model, text=str(content))


__all__ = [
    "OpenAICompatibleBackend",
    "OpenAIHttpError",
    "_classify_openai_error",
    "_http_error_from_exception",
    "_post_json",
]
