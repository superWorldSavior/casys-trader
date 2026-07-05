"""Transport LLM agnostique pour le brain trading.

Le module ne connaît pas le schéma de décision. Il expose seulement une API
`prompt -> texte` avec métadonnées fournisseur, puis `codex_client` valide le
JSON métier. Cela garde le fallback fournisseur hors de la stratégie.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from trader.support.system.process_env import sanitized_runtime_env

DEFAULT_SPARK_MODEL = "gpt-5.5"
DEFAULT_SPARK_FALLBACK_MODEL = "gpt-5.3-codex-spark"
DEFAULT_OLLAMA_BASE_URL = "https://ollama.com/v1"
DEFAULT_OLLAMA_MODEL = "nemotron-3-nano:30b-cloud"
DEFAULT_CONSOLIDATOR_OLLAMA_MODEL = "glm-5.1:cloud"
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_RUNTIME_SESSION_LABEL = "casys-trader:runtime-brain"
DEFAULT_CONSOLIDATOR_SESSION_LABEL = "casys-trader:learning-consolidator"

log = logging.getLogger(__name__)


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


def _acpx_global_flags(acpx_bin: str, *, model: str, timeout_s: int) -> list[str]:
    return [
        acpx_bin,
        "--format", "quiet",
        "--allowed-tools", "",
        "--no-terminal",
        "--non-interactive-permissions", "deny",
        "--model", model,
        "--timeout", str(timeout_s),
    ]


def _acpx_agent_part(agent: str | None) -> list[str]:
    return [] if not agent or agent == "default" else [agent]


def build_acpx_session_new_command(
    name: str,
    *,
    acpx_bin: str,
    model: str,
    timeout_s: int,
    agent: str | None = None,
) -> list[str]:
    return [
        *_acpx_global_flags(acpx_bin, model=model, timeout_s=timeout_s),
        *_acpx_agent_part(agent),
        "sessions",
        "new",
        "-s",
        name,
    ]


def build_acpx_session_prompt_command(
    name: str,
    prompt: str,
    *,
    acpx_bin: str,
    model: str,
    timeout_s: int,
    agent: str | None = None,
) -> list[str]:
    return [
        *_acpx_global_flags(acpx_bin, model=model, timeout_s=timeout_s),
        *_acpx_agent_part(agent),
        "prompt",
        "-s",
        name,
        prompt,
    ]


def build_acpx_session_close_command(
    name: str,
    *,
    acpx_bin: str,
    agent: str | None = None,
) -> list[str]:
    return [
        acpx_bin,
        "--format", "quiet",
        "--no-terminal",
        "--non-interactive-permissions", "deny",
        *_acpx_agent_part(agent),
        "sessions",
        "close",
        name,
    ]


def build_acpx_command(
    prompt: str,
    *,
    acpx_bin: str,
    model: str,
    timeout_s: int,
    agent: str | None = None,
    session_label: str | None = None,
) -> list[str]:
    labeled_prompt = _label_prompt(prompt, session_label=session_label)
    return [
        *_acpx_global_flags(acpx_bin, model=model, timeout_s=timeout_s),
        *_acpx_agent_part(agent),
        "exec",
        labeled_prompt,
    ]


def _clean_optional(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _label_prompt(prompt: str, *, session_label: str | None) -> str:
    cleaned = _clean_optional(session_label)
    if cleaned is None:
        return prompt
    return f"[{cleaned}]\n{prompt}"


def _default_acpx_session_label(provider: str) -> str | None:
    if provider == "consolidator":
        return DEFAULT_CONSOLIDATOR_SESSION_LABEL
    if provider == "acpx":
        return DEFAULT_RUNTIME_SESSION_LABEL
    return None


def _env_session_label(provider: str) -> str | None:
    if provider == "consolidator":
        return _env("TRADER_CONSOLIDATOR_ACPX_SESSION_LABEL", "TRADER_ACPX_SESSION_LABEL")
    return _env("TRADER_ACPX_SESSION_LABEL")


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


def _looks_retryable_acpx_error(*, provider: str, text: str) -> bool:
    if _looks_retryable_provider_error(text):
        return True
    if "internal error" in text.lower():
        return True
    # exit!=0 sans aucune sortie (stderr+stdout vides) = échec provider
    # transitoire (blip quota/dispo) qu'acpx remonte muet sous --format quiet ;
    # on le rend retryable pour autoriser le fallback plutôt qu'un HOLD sec.
    if not text.strip():
        return True
    return False


def _failure_code_from_text(text: str) -> str:
    lowered = text.lower()
    if "429" in lowered or "rate limit" in lowered or "rate_limit" in lowered:
        return "rate_limited"
    if "quota" in lowered or "credit" in lowered or "insufficient_quota" in lowered:
        return "quota_exceeded"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    return "provider_error"


def _terminate_process_group(pgid: int, *, grace_s: float = 2.0) -> None:
    if os.name != "posix":
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _run_one_shot_command(command: list[str], *, timeout_s: int) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
        env=sanitized_runtime_env(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc.pid)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        _terminate_process_group(proc.pid)

    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _run_and_parse(
    command: list[str],
    *,
    provider: str,
    model: str,
    acpx_bin: str,
    timeout_s: int,
) -> LlmCompletion | LlmFailure:
    if shutil.which(acpx_bin) is None:
        return LlmFailure(
            provider=provider,
            model=model,
            code="acpx_unavailable",
            message=f"binaire '{acpx_bin}' introuvable",
            retryable=True,
        )

    try:
        proc = _run_one_shot_command(command, timeout_s=timeout_s + 15)
    except subprocess.TimeoutExpired:
        return LlmFailure(
            provider=provider,
            model=model,
            code="timeout",
            message=f"> {timeout_s}s",
            retryable=False,
        )
    except Exception as exc:  # noqa: BLE001 - frontière fournisseur
        message = str(exc)
        return LlmFailure(
            provider=provider,
            model=model,
            code=_failure_code_from_text(message),
            message=message,
            retryable=_looks_retryable_provider_error(message),
        )

    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "")[:500]
        retryable = _looks_retryable_acpx_error(provider=provider, text=message)
        return LlmFailure(
            provider=provider,
            model=model,
            code=_failure_code_from_text(message) if retryable else "nonzero_exit",
            message=f"exit={proc.returncode} {message}",
            retryable=retryable,
        )

    return LlmCompletion(provider=provider, model=model, text=proc.stdout)


@dataclass(frozen=True)
class AcpxSession:
    provider: str
    model: str
    acpx_bin: str
    name: str
    agent: str | None = None

    def send(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        return _run_and_parse(
            build_acpx_session_prompt_command(
                self.name,
                prompt,
                acpx_bin=self.acpx_bin,
                model=self.model,
                timeout_s=timeout_s,
                agent=self.agent,
            ),
            provider=self.provider,
            model=self.model,
            acpx_bin=self.acpx_bin,
            timeout_s=timeout_s,
        )

    def close(self) -> None:
        try:
            _run_one_shot_command(
                build_acpx_session_close_command(
                    self.name,
                    acpx_bin=self.acpx_bin,
                    agent=self.agent,
                ),
                timeout_s=15,
            )
        except Exception as exc:  # noqa: BLE001 - fermeture best-effort, jamais bloquante
            log.warning("[acpx_session] close failed name=%s: %s", self.name, exc)
            return


class SessionProviderDown(Exception):
    def __init__(self, failure: LlmFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def session_complete_fn(session):
    def _complete(prompt: str, timeout_s: int) -> LlmCompletion | LlmFailure:
        result = session.send(prompt, timeout_s=timeout_s)
        if isinstance(result, LlmFailure) and result.retryable:
            raise SessionProviderDown(result)
        return result

    return _complete


def run_with_session_fallback(
    backends,
    *,
    task_id: str,
    resolve,
    open_timeout_s: int,
) -> object | LlmFailure:
    last_failure: LlmFailure | None = None
    for attempt, backend in enumerate(backends):
        session = backend.open_session(f"{task_id}:{attempt}", timeout_s=open_timeout_s)
        if isinstance(session, LlmFailure):
            last_failure = session
            continue
        try:
            return resolve(session)
        except SessionProviderDown as exc:
            last_failure = exc.failure
            continue
        finally:
            session.close()
    return last_failure or LlmFailure(
        provider="none",
        model="none",
        code="no_backend",
        message="aucun backend LLM configuré",
        retryable=False,
    )


@dataclass(frozen=True)
class AcpxBackend:
    provider: str = "acpx"
    model: str = DEFAULT_SPARK_MODEL
    acpx_bin: str = "acpx"
    agent: str | None = None
    session_label: str | None = DEFAULT_RUNTIME_SESSION_LABEL

    def open_session(self, name: str, *, timeout_s: int) -> AcpxSession | LlmFailure:
        res = _run_and_parse(
            build_acpx_session_new_command(
                name,
                acpx_bin=self.acpx_bin,
                model=self.model,
                timeout_s=timeout_s,
                agent=self.agent,
            ),
            provider=self.provider,
            model=self.model,
            acpx_bin=self.acpx_bin,
            timeout_s=timeout_s,
        )
        if not isinstance(res, LlmCompletion):
            return res

        return AcpxSession(
            provider=self.provider,
            model=self.model,
            acpx_bin=self.acpx_bin,
            name=name,
            agent=self.agent,
        )

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        return _run_and_parse(
            build_acpx_command(
                prompt,
                acpx_bin=self.acpx_bin,
                model=self.model,
                timeout_s=timeout_s,
                agent=self.agent,
                session_label=self.session_label,
            ),
            provider=self.provider,
            model=self.model,
            acpx_bin=self.acpx_bin,
            timeout_s=timeout_s,
        )


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


def _ollama_env(provider: str, suffix: str, *fallback_names: str, default: str | None = None) -> str | None:
    names: list[str] = []
    if provider == "consolidator":
        names.append(f"TRADER_CONSOLIDATOR_OLLAMA_{suffix}")
    names.extend(fallback_names)
    return _env(*names, default=default)


def build_default_router_from_env(
    *,
    env_path: str | Path | None = DEFAULT_ENV_PATH,
    acpx_bin: str = "acpx",
    spark_model: str = DEFAULT_SPARK_MODEL,
    spark_fallback_model: str | None = DEFAULT_SPARK_FALLBACK_MODEL,
    acpx_provider: str = "acpx",
    acpx_agent: str | None = None,
    acpx_session_label: str | None = None,
) -> LlmRouter:
    load_dotenv(env_path)
    # TRADER_ACPX_BIN prime sur le paramètre pour le provider de trading — mais PAS
    # pour le consolidateur dont le binaire est résolu en amont via
    # TRADER_CONSOLIDATOR_ACPX_BIN (les deux binaires sont indépendants selon .env.example).
    acpx_bin_env = os.getenv("TRADER_ACPX_BIN")
    if acpx_bin_env and acpx_provider != "consolidator":
        acpx_bin = acpx_bin_env
    session_label = (
        _clean_optional(acpx_session_label)
        or _env_session_label(acpx_provider)
        or _default_acpx_session_label(acpx_provider)
    )

    backends: list[LlmBackend] = [
        AcpxBackend(
            provider=acpx_provider,
            model=spark_model,
            acpx_bin=acpx_bin,
            agent=acpx_agent,
            session_label=session_label,
        )
    ]

    if acpx_provider != "consolidator":
        # Fallback de trade : Sonnet via `acpx claude` (le primary gpt-5.5 est épuisé ;
        # Sonnet exploite l'exploration là où spark restait inerte — backtest 2026-06-25,
        # 4 trades vs 1). Quand gpt-5.5 revient, il reprend la main en primary.
        # TRADER_SPARK_FALLBACK_MODEL surcharge le modèle ("" = désactive le tier).
        if "TRADER_SPARK_FALLBACK_MODEL" in os.environ:
            resolved_fallback = os.environ["TRADER_SPARK_FALLBACK_MODEL"]
        else:
            resolved_fallback = "sonnet"
        if resolved_fallback:
            backends.append(
                AcpxBackend(
                    provider="acpx-claude-sonnet",
                    model=resolved_fallback,
                    acpx_bin=acpx_bin,
                    agent="claude",
                    session_label=session_label,
                )
            )

    default_ollama_model = (
        DEFAULT_CONSOLIDATOR_OLLAMA_MODEL
        if acpx_provider == "consolidator"
        else DEFAULT_OLLAMA_MODEL
    )
    api_key = _ollama_env(acpx_provider, "API_KEY", "TRADER_OLLAMA_API_KEY", "OLLAMA_API_KEY")
    if api_key:
        backends.append(
            OpenAICompatibleBackend(
                provider="ollama-cloud",
                api_key=api_key,
                base_url=_ollama_env(
                    acpx_provider,
                    "BASE_URL",
                    "TRADER_OLLAMA_BASE_URL",
                    "OLLAMA_CLOUD_BASE_URL",
                    "OLLAMA_BASE_URL",
                    default=DEFAULT_OLLAMA_BASE_URL,
                )
                or DEFAULT_OLLAMA_BASE_URL,
                model=_ollama_env(
                    acpx_provider,
                    "MODEL",
                    "TRADER_OLLAMA_MODEL",
                    "OLLAMA_CLOUD_MODEL",
                    "OLLAMA_MODEL",
                    default=default_ollama_model,
                )
                or default_ollama_model,
            )
        )
    return LlmRouter(backends)
