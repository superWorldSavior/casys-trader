"""Backend-facing execution-profile projections."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from .acpx import (
    _acpx_profile_fingerprint,
    _acpx_transport_fingerprint,
    _agent_adapter_fingerprint,
    _effective_agent_invocation,
    _profile_config_identity,
)
from .common import _clean


def _provider_endpoint_fingerprint(backend: object) -> str | None:
    """Hash a causal endpoint projection without credentials or URL secrets."""

    base_url = _clean(getattr(backend, "base_url", None))
    if base_url is None:
        return None
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    effective_port = port or (443 if scheme == "https" else 80)
    causal_endpoint = {
        "backend": type(backend).__name__,
        "scheme": scheme,
        "host": host,
        "port": effective_port,
        "path": parsed.path.rstrip("/") or "/",
    }
    canonical = json.dumps(causal_endpoint, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def model_profiles_from_backends(backends: object, *, repo_root: str | Path) -> dict[str, dict[str, object]]:
    """Capture effective, non-secret model execution profiles at daemon boot."""

    profiles: dict[str, dict[str, object]] = {}
    acpx_transport_cache: dict[str, str | None] = {}
    candidates = backends if isinstance(backends, (list, tuple)) else ()
    for backend in candidates:
        provider = _clean(getattr(backend, "provider", None))
        model = _clean(getattr(backend, "model", None))
        if provider is None:
            continue
        if type(backend).__name__ == "AcpxBackend":
            invocation = _effective_agent_invocation(backend)
            configured_agent = _clean(getattr(backend, "agent", None)) or "default"
            agent = invocation[0] if invocation is not None else configured_agent.lower()
            profile_effort, agent_profile_fingerprint = _profile_config_identity(
                backend, repo_root=Path(repo_root), agent=agent
            )
            configured_acpx_bin = _clean(getattr(backend, "acpx_bin", None)) or ""
            if configured_acpx_bin not in acpx_transport_cache:
                acpx_transport_cache[configured_acpx_bin] = _acpx_transport_fingerprint(backend)
            profile = {
                "configured_model": model,
                "transport": "acpx",
                "agent": agent,
                "reasoning_effort": _clean(getattr(backend, "reasoning_effort", None)) or profile_effort,
                "profile_fingerprint": _acpx_profile_fingerprint(
                    agent_profile=agent_profile_fingerprint,
                    transport=acpx_transport_cache[configured_acpx_bin],
                    adapter=_agent_adapter_fingerprint(invocation) if invocation is not None else None,
                ),
            }
        else:
            profile = {
                "configured_model": model,
                "transport": "openai-compatible",
                "agent": None,
                "reasoning_effort": None,
                "profile_fingerprint": _provider_endpoint_fingerprint(backend),
            }
        profiles[provider] = profile
    return profiles
