"""Port LLM agnostique pour le brain trading.

Le module ne connaît pas le schéma de décision. Il expose le contrat
`prompt -> texte` avec métadonnées fournisseur, le router de fallback et la
factory d'environnement. Les adaptateurs I/O vivent sous
``trader.infrastructure.llm`` et sont ré-exportés ici par compatibilité.
"""

from __future__ import annotations

import importlib
import os
from enum import Enum
from pathlib import Path

from trader.domain.llm import (
    LlmBackend as LlmBackend,
    LlmCompletion,
    LlmExecutionCapability as LlmExecutionCapability,
    LlmFailure,
    LlmRouter as LlmRouter,
    LlmSession as LlmSession,
    SessionLlmBackend as SessionLlmBackend,
    execution_capability_of as execution_capability_of,
)

DEFAULT_TRADER_MODEL = "gpt-5.6-terra"
DEFAULT_ANALYST_MODEL = "gpt-5.6-sol"
# Compatibility name for callers that still describe the primary trader model
# as "spark". Role-specific agents must use DEFAULT_ANALYST_MODEL explicitly.
DEFAULT_SPARK_MODEL = DEFAULT_TRADER_MODEL
DEFAULT_SPARK_FALLBACK_MODEL = "gpt-5.3-codex-spark"
DEFAULT_FALLBACK_AGENT = "claude"
DEFAULT_FALLBACK_MODEL = "sonnet"
CURSOR_TRANSPORT_PROVIDER = "cursor-agent"
_XAI_FALLBACK_AGENTS = frozenset({"grok-build", "grok"})
DEFAULT_OLLAMA_BASE_URL = "https://ollama.com/v1"
DEFAULT_OLLAMA_MODEL = "nemotron-3-nano:30b-cloud"
DEFAULT_CONSOLIDATOR_OLLAMA_MODEL = "glm-5.1:cloud"
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_RUNTIME_SESSION_LABEL = "casys-trader:runtime-brain"
DEFAULT_CONSOLIDATOR_SESSION_LABEL = "casys-trader:learning-consolidator"


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


class RoleFallbackPolicy(str, Enum):
    """Explicit secondary-backend contract for one LLM role.

    Cursor as primary never auto-appends Claude/Sonnet. It reuses the
    configured xAI pair when that pair is present, otherwise it has no
    fallback. Other primaries keep the historical Sonnet default where that
    contract still applies (Brain + universe/company-micro, not consolidator).
    """

    NONE = "none"
    CONFIGURED_XAI = "configured_xai"
    LEGACY_SONNET = "legacy_sonnet"

    @classmethod
    def for_role(cls, *, primary_agent: str | None, acpx_provider: str) -> RoleFallbackPolicy:
        if str(primary_agent or "").strip().lower() == "cursor":
            return cls.CONFIGURED_XAI
        if acpx_provider == "consolidator":
            return cls.NONE
        return cls.LEGACY_SONNET


def _fallback_model_from_env(*, default: str | None) -> str | None:
    if "TRADER_FALLBACK_MODEL" in os.environ:
        return os.environ["TRADER_FALLBACK_MODEL"]
    if "TRADER_SPARK_FALLBACK_MODEL" in os.environ:
        return os.environ["TRADER_SPARK_FALLBACK_MODEL"]
    return default


def _runtime_brain_fallback_from_env() -> tuple[str, str, str | None]:
    """Resolve the optional secondary ACP backend for a non-Cursor Brain.

    ``TRADER_SPARK_FALLBACK_MODEL`` remains a model-only compatibility alias.
    An explicitly empty model disables tier 2.
    """

    agent = _clean_optional(os.getenv("TRADER_FALLBACK_ACPX_AGENT")) or DEFAULT_FALLBACK_AGENT
    model = _fallback_model_from_env(default=DEFAULT_FALLBACK_MODEL)
    provider = {
        "claude": "acpx-claude-sonnet",
        "cursor": CURSOR_TRANSPORT_PROVIDER,
    }.get(agent.lower(), "acpx-custom-fallback")
    return provider, agent, model


def _configured_xai_fallback() -> tuple[str, str, str] | None:
    """Configured grok-build pair, or none. Never Claude/Sonnet or Cursor."""

    agent = _clean_optional(os.getenv("TRADER_FALLBACK_ACPX_AGENT"))
    model = _fallback_model_from_env(default=None)
    if agent is None or model is None or not str(model).strip():
        return None
    if agent.lower() not in _XAI_FALLBACK_AGENTS:
        return None
    return "acpx-custom-fallback", agent, model


def _default_acpx_session_label(provider: str) -> str | None:
    if provider == "consolidator":
        return DEFAULT_CONSOLIDATOR_SESSION_LABEL
    if provider == "acpx":
        return DEFAULT_RUNTIME_SESSION_LABEL
    return None


# Profil Codex par rôle, conservé comme frontière d'isolation. La configuration
# active pointe tous les rôles sur le même CODEX_HOME low ; le brain applique
# son effort medium via l'option ACP de sa session.
# Absent → `CODEX_HOME` global (résolu par le transport), comportement historique.
_ROLE_CODEX_HOME_ENV = {
    "acpx": "TRADER_CODEX_HOME",
    "consolidator": "TRADER_CONSOLIDATOR_CODEX_HOME",
    "universe": "TRADER_UNIVERSE_CODEX_HOME",
    "company-micro": "TRADER_COMPANY_MICRO_CODEX_HOME",
}

# Profil Grok par rôle. L'effort Grok n'est PAS passable par appel ACP
# (`session/set_config_option` → -32601) : un effort = un GROK_HOME.
# Brain → ops/grok-home (low) ; analystes → ops/grok-home-medium.
# Absent → `GROK_HOME` global, puis ops/grok-home (transport).
_ROLE_GROK_HOME_ENV = {
    "acpx": "TRADER_GROK_HOME",
    "consolidator": "TRADER_CONSOLIDATOR_GROK_HOME",
    "universe": "TRADER_UNIVERSE_GROK_HOME",
    "company-micro": "TRADER_COMPANY_MICRO_GROK_HOME",
}


def _env_codex_home(provider: str) -> str | None:
    """Profil Codex du rôle, ou None pour retomber sur le ``CODEX_HOME`` global.

    L'analyste macro/news partage volontairement le profil du consolidateur
    (il partage déjà son agent et son binaire, cf. ``news_macro.analyzer``).
    """

    name = _ROLE_CODEX_HOME_ENV.get(provider)
    return _clean_optional(os.getenv(name)) if name else None


def _env_grok_home(provider: str) -> str | None:
    """Profil Grok du rôle, ou None pour retomber sur le ``GROK_HOME`` global.

    Même partage macro/news → consolidateur que ``_env_codex_home``.
    """

    name = _ROLE_GROK_HOME_ENV.get(provider)
    return _clean_optional(os.getenv(name)) if name else None


def _env_session_label(provider: str) -> str | None:
    if provider == "consolidator":
        return _env("TRADER_CONSOLIDATOR_ACPX_SESSION_LABEL", "TRADER_ACPX_SESSION_LABEL")
    if provider == "universe":
        return _env("TRADER_UNIVERSE_ACPX_SESSION_LABEL", "TRADER_ACPX_SESSION_LABEL")
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
    from trader.infrastructure.llm.cursor_backend import CursorAgentBackend
    from trader.infrastructure.llm.openai_backend import OpenAICompatibleBackend

    load_dotenv(env_path)
    # TRADER_ACPX_BIN prime sur le paramètre pour le provider de trading — mais PAS
    # pour le consolidateur dont le binaire est résolu en amont via
    # TRADER_CONSOLIDATOR_ACPX_BIN (les deux binaires sont indépendants selon .env.example).
    acpx_bin_env = (
        os.getenv("TRADER_UNIVERSE_ACPX_BIN") or os.getenv("TRADER_ACPX_BIN")
        if acpx_provider == "universe"
        else os.getenv("TRADER_ACPX_BIN")
    )
    if acpx_bin_env and acpx_provider != "consolidator":
        acpx_bin = acpx_bin_env
    # Knobs env du brain trader, alignés sur le pattern des agents analystes
    # (TRADER_UNIVERSE_*, TRADER_COMPANY_MICRO_*, TRADER_CONSOLIDATOR_*).
    # Restreints au profil trader par défaut (provider "acpx" au modèle défaut)
    # pour ne pas fuiter vers les rôles analystes qui partagent ce builder avec
    # un autre modèle explicite (rotation, universe). L'agent explicite prime
    # toujours ; TRADER_MODEL ne remplace que le défaut, jamais un choix caller.
    is_runtime_brain = acpx_provider == "acpx" and spark_model == DEFAULT_SPARK_MODEL
    if is_runtime_brain:
        acpx_agent = acpx_agent or _clean_optional(os.getenv("TRADER_ACPX_AGENT"))
        spark_model = _clean_optional(os.getenv("TRADER_MODEL")) or spark_model
    session_label = (
        _clean_optional(acpx_session_label)
        or _env_session_label(acpx_provider)
        or _default_acpx_session_label(acpx_provider)
    )

    codex_home = _env_codex_home(acpx_provider)
    grok_home = _env_grok_home(acpx_provider)
    reasoning_effort = _clean_optional(os.getenv("TRADER_REASONING_EFFORT")) if is_runtime_brain else None

    def build_agent_backend(
        *,
        provider: str,
        model: str,
        agent: str | None,
        is_fallback: bool = False,
    ) -> LlmBackend:
        if str(agent or "").strip().lower() == "cursor":
            return CursorAgentBackend(
                provider=CURSOR_TRANSPORT_PROVIDER,
                model=model,
                session_label=session_label,
            )
        return AcpxBackend(
            provider=provider,
            model=model,
            acpx_bin=acpx_bin,
            agent=agent,
            session_label=session_label,
            codex_home=codex_home,
            grok_home=grok_home,
            reasoning_effort=reasoning_effort,
            # A secondary provider never inherits native tools or writes.
            allow_native_exec=False if is_fallback else None,
        )

    backends: list[LlmBackend] = [build_agent_backend(provider=acpx_provider, model=spark_model, agent=acpx_agent)]

    policy = RoleFallbackPolicy.for_role(primary_agent=acpx_agent, acpx_provider=acpx_provider)
    fallback_route: tuple[str, str, str | None] | None = None
    if policy is RoleFallbackPolicy.CONFIGURED_XAI:
        fallback_route = _configured_xai_fallback()
    elif policy is RoleFallbackPolicy.LEGACY_SONNET:
        if is_runtime_brain:
            fallback_route = _runtime_brain_fallback_from_env()
        else:
            fallback_route = ("acpx-claude-sonnet", "claude", DEFAULT_FALLBACK_MODEL)
    if fallback_route is not None:
        fallback_provider, fallback_agent, resolved_fallback = fallback_route
        if resolved_fallback:
            backends.append(
                build_agent_backend(
                    provider=fallback_provider,
                    model=resolved_fallback,
                    agent=fallback_agent,
                    is_fallback=True,
                )
            )

    default_ollama_model = (
        DEFAULT_CONSOLIDATOR_OLLAMA_MODEL if acpx_provider == "consolidator" else DEFAULT_OLLAMA_MODEL
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
    "CursorAgentBackend": "trader.infrastructure.llm.cursor_backend",
    "CursorAgentSession": "trader.infrastructure.llm.cursor_backend",
    "build_cursor_agent_command": "trader.infrastructure.llm.cursor_backend",
    "SessionProviderDown": "trader.infrastructure.llm.acpx_backend",
    "build_acpx_command": "trader.infrastructure.llm.acpx_backend",
    "build_acpx_session_close_command": "trader.infrastructure.llm.acpx_backend",
    "build_acpx_session_config_command": "trader.infrastructure.llm.acpx_backend",
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
    "DEFAULT_ANALYST_MODEL",
    "CURSOR_TRANSPORT_PROVIDER",
    "DEFAULT_FALLBACK_AGENT",
    "DEFAULT_FALLBACK_MODEL",
    "LlmExecutionCapability",
    "RoleFallbackPolicy",
    "execution_capability_of",
    "DEFAULT_CONSOLIDATOR_SESSION_LABEL",
    "DEFAULT_ENV_PATH",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_RUNTIME_SESSION_LABEL",
    "DEFAULT_SPARK_FALLBACK_MODEL",
    "DEFAULT_SPARK_MODEL",
    "DEFAULT_TRADER_MODEL",
    "LlmBackend",
    "LlmCompletion",
    "LlmFailure",
    "LlmRouter",
    "LlmSession",
    "SessionLlmBackend",
    "build_default_router_from_env",
    "load_dotenv",
    *_LAZY_EXPORT_MODULES,
]
