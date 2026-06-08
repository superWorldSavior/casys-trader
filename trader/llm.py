"""Transport LLM agnostique pour le brain trading.

Le module ne connaît pas le schéma de décision. Il expose seulement une API
`prompt -> texte` avec métadonnées fournisseur, puis `codex_client` valide le
JSON métier. Cela garde le fallback fournisseur hors de la stratégie.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

DEFAULT_SPARK_MODEL = "gpt-5.3-codex-spark/medium"
DEFAULT_OLLAMA_BASE_URL = "https://ollama.com/v1"
DEFAULT_OLLAMA_MODEL = "nemotron-3-nano:30b-cloud"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


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

            failure = result
            last_failure = failure
            has_next = index < len(self.backends) - 1
            if has_next and failure.retryable:
                fallback_reason = failure.fallback_reason or f"{failure.provider}:{failure.code}"
                continue
            if fallback_reason and failure.fallback_reason is None:
                return replace(failure, fallback_reason=fallback_reason)
            return failure

        return last_failure or LlmFailure(
            provider="none",
            model="none",
            code="no_backend",
            message="aucun backend LLM configuré",
            retryable=False,
        )


def build_acpx_command(prompt: str, *, acpx_bin: str, model: str, timeout_s: int) -> list[str]:
    return [
        acpx_bin,
        "--format", "quiet",
        "--allowed-tools", "",
        "--no-terminal",
        "--non-interactive-permissions", "deny",
        "--model", model,
        "--timeout", str(timeout_s),
        "exec",
        prompt,
    ]


def _looks_retryable_provider_error(text: str) -> bool:
    lowered = text.lower()
    needles = (
        "429",
        "rate limit",
        "rate_limit",
        "quota",
        "credit",
        "credits",
        "insufficient_quota",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "overloaded",
        "provider",
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


@dataclass(frozen=True)
class AcpxBackend:
    provider: str = "spark"
    model: str = DEFAULT_SPARK_MODEL
    acpx_bin: str = "acpx"

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        if shutil.which(self.acpx_bin) is None:
            return LlmFailure(
                provider=self.provider,
                model=self.model,
                code="acpx_unavailable",
                message=f"binaire '{self.acpx_bin}' introuvable",
                retryable=True,
            )

        try:
            proc = subprocess.run(
                build_acpx_command(prompt, acpx_bin=self.acpx_bin, model=self.model, timeout_s=timeout_s),
                capture_output=True,
                text=True,
                timeout=timeout_s + 15,
            )
        except subprocess.TimeoutExpired:
            return LlmFailure(
                provider=self.provider,
                model=self.model,
                code="timeout",
                message=f"> {timeout_s}s",
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001 - frontière fournisseur
            message = str(exc)
            return LlmFailure(
                provider=self.provider,
                model=self.model,
                code=_failure_code_from_text(message),
                message=message,
                retryable=_looks_retryable_provider_error(message),
            )

        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "")[:500]
            retryable = _looks_retryable_provider_error(message)
            return LlmFailure(
                provider=self.provider,
                model=self.model,
                code=_failure_code_from_text(message) if retryable else "nonzero_exit",
                message=f"exit={proc.returncode} {message}",
                retryable=retryable,
            )

        return LlmCompletion(provider=self.provider, model=self.model, text=proc.stdout)


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

    if http_error.status == 429:
        return "rate_limited", True, http_error.body
    if http_error.status in {408, 500, 502, 503, 504} or http_error.status == 0:
        return "provider_error", True, http_error.body
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


def load_dotenv(path: str | Path | None = DEFAULT_ENV_PATH, *, override: bool = False) -> None:
    if path is None:
        return
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def build_default_router_from_env(
    *,
    env_path: str | Path | None = DEFAULT_ENV_PATH,
    acpx_bin: str = "acpx",
    spark_model: str = DEFAULT_SPARK_MODEL,
) -> LlmRouter:
    load_dotenv(env_path)

    backends: list[LlmBackend] = [AcpxBackend(model=spark_model, acpx_bin=acpx_bin)]
    api_key = _env("TRADER_OLLAMA_API_KEY", "OLLAMA_API_KEY")
    if api_key:
        backends.append(
            OpenAICompatibleBackend(
                provider="ollama-cloud",
                api_key=api_key,
                base_url=_env("TRADER_OLLAMA_BASE_URL", "OLLAMA_CLOUD_BASE_URL", "OLLAMA_BASE_URL", default=DEFAULT_OLLAMA_BASE_URL) or DEFAULT_OLLAMA_BASE_URL,
                model=_env("TRADER_OLLAMA_MODEL", "OLLAMA_CLOUD_MODEL", "OLLAMA_MODEL", default=DEFAULT_OLLAMA_MODEL) or DEFAULT_OLLAMA_MODEL,
            )
        )
    return LlmRouter(backends)
