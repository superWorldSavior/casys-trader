"""Port LLM agnostique pour le brain trading.

Le module ne connaît pas le schéma de décision. Il expose le contrat
`prompt -> texte` avec métadonnées fournisseur, le router de fallback et la
factory d'environnement. Les adaptateurs I/O vivent sous
``trader.infrastructure.llm`` et sont ré-exportés ici par compatibilité.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from trader.domain.llm import LlmCompletion, LlmFailure

DEFAULT_SPARK_MODEL = "gpt-5.5"
DEFAULT_SPARK_FALLBACK_MODEL = "gpt-5.3-codex-spark"
DEFAULT_OLLAMA_BASE_URL = "https://ollama.com/v1"
DEFAULT_OLLAMA_MODEL = "nemotron-3-nano:30b-cloud"
DEFAULT_CONSOLIDATOR_OLLAMA_MODEL = "glm-5.1:cloud"
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_RUNTIME_SESSION_LABEL = "casys-trader:runtime-brain"
DEFAULT_CONSOLIDATOR_SESSION_LABEL = "casys-trader:learning-consolidator"


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


def _clean_optional(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


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
    from trader.infrastructure.llm.acpx_backend import AcpxBackend
    from trader.infrastructure.llm.openai_backend import OpenAICompatibleBackend

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


_LAZY_EXPORT_MODULES = {
    "AcpxBackend": "trader.infrastructure.llm.acpx_backend",
    "AcpxSession": "trader.infrastructure.llm.acpx_backend",
    "SessionProviderDown": "trader.infrastructure.llm.acpx_backend",
    "build_acpx_command": "trader.infrastructure.llm.acpx_backend",
    "build_acpx_session_close_command": "trader.infrastructure.llm.acpx_backend",
    "build_acpx_session_new_command": "trader.infrastructure.llm.acpx_backend",
    "build_acpx_session_prompt_command": "trader.infrastructure.llm.acpx_backend",
    "run_with_session_fallback": "trader.infrastructure.llm.acpx_backend",
    "session_complete_fn": "trader.infrastructure.llm.acpx_backend",
    "_per_call_timeout_cap_s": "trader.infrastructure.llm.acpx_backend",
    "_run_and_parse": "trader.infrastructure.llm.acpx_backend",
    "_run_one_shot_command": "trader.infrastructure.llm.acpx_backend",
    "_terminate_process_group": "trader.infrastructure.llm.acpx_backend",
    "OpenAICompatibleBackend": "trader.infrastructure.llm.openai_backend",
    "OpenAIHttpError": "trader.infrastructure.llm.openai_backend",
    "_classify_openai_error": "trader.infrastructure.llm.openai_backend",
    "_http_error_from_exception": "trader.infrastructure.llm.openai_backend",
    "_post_json": "trader.infrastructure.llm.openai_backend",
    "_failure_code_from_text": "trader.infrastructure.llm._errors",
    "_looks_retryable_provider_error": "trader.infrastructure.llm._errors",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "DEFAULT_CONSOLIDATOR_OLLAMA_MODEL",
    "DEFAULT_CONSOLIDATOR_SESSION_LABEL",
    "DEFAULT_ENV_PATH",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_RUNTIME_SESSION_LABEL",
    "DEFAULT_SPARK_FALLBACK_MODEL",
    "DEFAULT_SPARK_MODEL",
    "LlmBackend",
    "LlmCompletion",
    "LlmFailure",
    "LlmRouter",
    "build_default_router_from_env",
    "load_dotenv",
    *_LAZY_EXPORT_MODULES,
]
