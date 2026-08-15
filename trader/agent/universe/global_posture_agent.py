"""LLM-backed adapter for the global universe posture."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from trader.agent import llm
from trader.agent.universe.agent import (
    UniverseAgentError,
    UniverseAgentPayloadError,
    build_universe_router_from_env,
    universe_timeout_s,
)
from trader.agent.universe.global_posture_prompt import (
    build_global_posture_prompt,
    build_global_posture_repair_prompt,
    parse_global_posture_completion,
)
from trader.domain.universe.global_posture import GlobalUniversePosture


@dataclass(frozen=True)
class GlobalUniversePostureRequest:
    """Bounded input for one global universe-posture pass."""

    as_of: str
    global_family_board: Any = field(default_factory=dict)
    global_situation_digest: Any = field(default_factory=dict)
    sticky: tuple[str, ...] = ()
    venues: tuple[str, ...] = ()


class LlmGlobalPostureAgent:
    """Compose the advisory cross-region universe posture."""

    def __init__(
        self,
        router: llm.LlmRouter | None = None,
        *,
        timeout_s: int | None = None,
    ) -> None:
        self._router = router or build_universe_router_from_env()
        self._timeout_s = universe_timeout_s(timeout_s)

    def compose(self, request: GlobalUniversePostureRequest) -> GlobalUniversePosture:
        prompt = build_global_posture_prompt(
            global_family_board=request.global_family_board,
            global_situation_digest=request.global_situation_digest,
            sticky=request.sticky,
            venues=request.venues,
            as_of=request.as_of,
        )
        completion = self._router.complete(prompt, timeout_s=self._timeout_s)
        if isinstance(completion, llm.LlmFailure):
            raise UniverseAgentError(
                completion.code,
                completion.message,
                provider=completion.provider,
                model=completion.model,
                provider_fallback_reason=completion.fallback_reason,
            )
        posture, error = parse_global_posture_completion(completion.text)
        if posture is None:
            repair = build_global_posture_repair_prompt(
                global_family_board=request.global_family_board,
                global_situation_digest=request.global_situation_digest,
                sticky=request.sticky,
                venues=request.venues,
                as_of=request.as_of,
                invalid_response=completion.text,
                parse_error=error or "invalid_global_posture_response",
            )
            completion = self._router.complete(repair, timeout_s=self._timeout_s)
            if isinstance(completion, llm.LlmFailure):
                raise UniverseAgentError(
                    completion.code,
                    completion.message,
                    provider=completion.provider,
                    model=completion.model,
                    provider_fallback_reason=completion.fallback_reason,
                )
            posture, error = parse_global_posture_completion(completion.text)
            if posture is None:
                raise UniverseAgentPayloadError(
                    error or "invalid_global_posture_response",
                    provider=completion.provider,
                    model=completion.model,
                    provider_fallback_reason=completion.fallback_reason,
                )
        if not posture.as_of:
            as_of = str(request.as_of or "").strip()
            if not as_of:
                raise UniverseAgentPayloadError(
                    "as_of_missing",
                    provider=completion.provider,
                    model=completion.model,
                    provider_fallback_reason=completion.fallback_reason,
                )
            posture = replace(posture, as_of=as_of)
        missing_venues = _missing_venues(request.venues, posture.venue_posture)
        if missing_venues:
            raise UniverseAgentPayloadError(
                f"venue_posture_missing_venues:{','.join(missing_venues)}",
                provider=completion.provider,
                model=completion.model,
                provider_fallback_reason=completion.fallback_reason,
            )
        return posture


def _missing_venues(
    venues: tuple[str, ...],
    venue_posture: Mapping[str, str],
) -> tuple[str, ...]:
    requested = tuple(str(venue or "").strip() for venue in venues if str(venue or "").strip())
    if not requested:
        return ()
    present = set(venue_posture)
    return tuple(venue for venue in requested if venue not in present)


__all__ = [
    "GlobalUniversePostureRequest",
    "LlmGlobalPostureAgent",
]
