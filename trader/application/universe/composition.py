"""Application contract for one venue universe-composition pass."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from trader.domain.universe import (
    UniverseSituationContext,
    build_family_snapshot,
    candidate_scope_id,
    enrich_candidates_with_family,
)

HOTLIST_CAP = 25
UniverseCompositionStatus = Literal["success", "invalid", "error"]


@dataclass(frozen=True)
class UniverseCompositionRequest:
    """All bounded inputs for one venue's universe agent."""

    venue: str
    as_of: str
    candidate_scope_id: str
    candidates: tuple[dict[str, Any], ...]
    baseline: tuple[str, ...]
    sticky: tuple[str, ...]
    market_context: dict[str, Any]
    situation_context: UniverseSituationContext
    family_snapshot: dict[str, dict[str, Any]]
    global_family_board: dict[str, Any] = field(default_factory=dict)
    retrieval_refs: tuple[str, ...] = ()
    retrieval_status: str = "not_enabled"

    @property
    def candidate_symbols(self) -> tuple[str, ...]:
        return tuple(candidate["symbol"] for candidate in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "as_of": self.as_of,
            "candidate_scope_id": self.candidate_scope_id,
            "candidates": _json_copy(list(self.candidates)),
            "baseline": list(self.baseline),
            "sticky": list(self.sticky),
            "market_context": _json_copy(self.market_context),
            "situation_context": self.situation_context.to_dict(),
            "family_snapshot": _json_copy(self.family_snapshot),
            "global_family_board": _json_copy(self.global_family_board),
            "retrieval_refs": list(self.retrieval_refs),
            "retrieval_status": self.retrieval_status,
        }


@dataclass(frozen=True)
class UniverseAgentDecision:
    """Parsed agent proposal before application validation."""

    selected_hotlist: tuple[str, ...]
    summary: str
    family_postures: dict[str, str]
    symbol_rationales: dict[str, str]
    contract_version: str
    provider: str | None = None
    model: str | None = None
    provider_fallback_reason: str | None = None


@dataclass(frozen=True)
class UniverseCompositionResult:
    """Explicit outcome; invalid/error never imply an automatic fallback."""

    status: UniverseCompositionStatus
    decision: UniverseAgentDecision | None = None
    validation_errors: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    fallback_used: bool = False
    agent_provider: str | None = None
    agent_model: str | None = None
    agent_provider_fallback_reason: str | None = None


@runtime_checkable
class UniverseCompositionAgent(Protocol):
    def compose(self, request: UniverseCompositionRequest) -> UniverseAgentDecision:
        ...


def build_universe_composition_request(
    *,
    venue: str,
    as_of: str,
    candidates: Iterable[Mapping[str, Any]],
    baseline: Iterable[str],
    sticky: Iterable[str],
    market_context: Mapping[str, Any] | None,
    situation_context: UniverseSituationContext,
    global_family_board: Mapping[str, Any] | None = None,
    retrieval_refs: Iterable[str] = (),
    retrieval_status: str = "not_enabled",
) -> UniverseCompositionRequest:
    """Normalize and validate one venue request before any agent call."""

    normalized_venue = str(venue or "").strip().upper()
    if not normalized_venue:
        raise ValueError("venue_required")
    normalized_as_of = str(as_of or "").strip()
    if not normalized_as_of:
        raise ValueError("as_of_required")

    enriched = enrich_candidates_with_family(candidates)
    candidate_symbols = [candidate["symbol"] for candidate in enriched]
    if not candidate_symbols:
        raise ValueError("candidate_pool_empty")
    if len(candidate_symbols) != len(set(candidate_symbols)):
        raise ValueError("duplicate_candidate_symbol")

    raw_baseline = [str(symbol or "").strip() for symbol in baseline if str(symbol or "").strip()]
    if len(raw_baseline) != len(set(raw_baseline)):
        raise ValueError("duplicate_baseline_symbol")
    normalized_baseline = tuple(raw_baseline)
    normalized_sticky = tuple(sorted(_unique_symbols(sticky)))
    if len(normalized_baseline) > HOTLIST_CAP:
        raise ValueError("baseline_cap_exceeded")
    pool = set(candidate_symbols)
    outside = [symbol for symbol in normalized_baseline if symbol not in pool]
    if outside:
        raise ValueError(f"baseline_outside_pool:{','.join(outside)}")
    sticky_in_baseline = [symbol for symbol in normalized_baseline if symbol in normalized_sticky]
    if sticky_in_baseline:
        raise ValueError(f"baseline_contains_sticky:{','.join(sticky_in_baseline)}")

    normalized_retrieval_refs = tuple(_unique_symbols(retrieval_refs))
    normalized_retrieval_status = str(retrieval_status or "").strip()
    if normalized_retrieval_status == "not_enabled" and normalized_retrieval_refs:
        raise ValueError("retrieval_refs_must_be_empty_when_not_enabled")
    if normalized_retrieval_status != "not_enabled":
        raise ValueError("retrieval_status_not_supported")
    normalized_global_family_board = dict(global_family_board or {})
    if normalized_global_family_board and normalized_global_family_board.get(
        "role"
    ) != "comparative_context_not_capital_allocation":
        raise ValueError("global_family_board_role_invalid")

    snapshot = build_family_snapshot(enriched, normalized_baseline, normalized_sticky)
    normalized_market_context = dict(market_context or {})
    regime_families = normalized_market_context.get("regime_families")
    if not isinstance(regime_families, Mapping):
        regime_families = {}
    for family, family_state in snapshot.items():
        family_points = situation_context.families.get(family)
        if family_points:
            situation_status = "observed"
        elif family in situation_context.families:
            situation_status = (
                "truncated_or_no_evidence" if situation_context.truncated else "no_evidence"
            )
        else:
            situation_status = "not_reported"
        family_state["situation_status"] = situation_status
        regime = regime_families.get(family)
        family_state["regime_status"] = "observed" if isinstance(regime, Mapping) else "missing"
        family_state["regime"] = _json_copy(dict(regime)) if isinstance(regime, Mapping) else None
    scope_id = candidate_scope_id(
        normalized_venue,
        enriched,
        normalized_baseline,
        normalized_as_of,
    )
    return UniverseCompositionRequest(
        venue=normalized_venue,
        as_of=normalized_as_of,
        candidate_scope_id=scope_id,
        candidates=enriched,
        baseline=normalized_baseline,
        sticky=normalized_sticky,
        market_context=_json_copy(normalized_market_context),
        situation_context=situation_context,
        family_snapshot=snapshot,
        global_family_board=_json_copy(normalized_global_family_board),
        retrieval_refs=normalized_retrieval_refs,
        retrieval_status=normalized_retrieval_status,
    )


def validate_universe_decision(
    request: UniverseCompositionRequest,
    decision: UniverseAgentDecision,
) -> tuple[str, ...]:
    """Return every explicit contract violation in stable order."""

    if not isinstance(decision, UniverseAgentDecision):
        return ("invalid_agent_decision_type",)
    if not isinstance(decision.selected_hotlist, (list, tuple)):
        return ("selected_hotlist_not_sequence",)
    errors: list[str] = []
    selected = tuple(str(symbol or "").strip() for symbol in decision.selected_hotlist)
    if not selected or any(not symbol for symbol in selected):
        errors.append("selected_hotlist_empty")
    if len(selected) > HOTLIST_CAP:
        errors.append("selected_hotlist_cap_exceeded")
    duplicate_symbols = sorted(symbol for symbol in set(selected) if selected.count(symbol) > 1)
    errors.extend(f"duplicate_selected_symbol:{symbol}" for symbol in duplicate_symbols)

    pool = set(request.candidate_symbols)
    sticky = set(request.sticky)
    errors.extend(f"selection_outside_pool:{symbol}" for symbol in selected if symbol not in pool)
    errors.extend(f"selection_contains_sticky:{symbol}" for symbol in selected if symbol in sticky)

    if not str(decision.summary or "").strip():
        errors.append("summary_required")
    if decision.contract_version != "universe.v1":
        errors.append("unsupported_contract_version")

    if not isinstance(decision.symbol_rationales, Mapping):
        errors.append("symbol_rationales_not_mapping")
        rationales: dict[str, str] = {}
    else:
        rationales = {
            str(symbol or "").strip(): str(reason or "").strip()
            for symbol, reason in decision.symbol_rationales.items()
        }
    for symbol in selected:
        if not rationales.get(symbol):
            errors.append(f"missing_symbol_rationale:{symbol}")
    for symbol in sorted(set(rationales) - set(selected)):
        errors.append(f"rationale_outside_selection:{symbol}")

    if not isinstance(decision.family_postures, Mapping):
        errors.append("family_postures_not_mapping")
        family_postures: dict[str, str] = {}
    else:
        family_postures = {
            str(family or "").strip(): str(posture or "").strip()
            for family, posture in decision.family_postures.items()
        }
        for family, posture in sorted(decision.family_postures.items()):
            if not str(family or "").strip() or not str(posture or "").strip():
                errors.append("invalid_family_posture")
                break
    family_by_symbol = {
        candidate["symbol"]: candidate["family"] for candidate in request.candidates
    }
    for family in sorted(
        {
            family_by_symbol[symbol]
            for symbol in selected
            if symbol in family_by_symbol
        }
    ):
        if not family_postures.get(family):
            errors.append(f"missing_family_posture:{family}")
    return tuple(dict.fromkeys(errors))


def compose_universe(
    request: UniverseCompositionRequest,
    *,
    agent: UniverseCompositionAgent,
) -> UniverseCompositionResult:
    """Run one agent composition with no hidden deterministic fallback."""

    try:
        decision = agent.compose(request)
    except ValueError as exc:
        return UniverseCompositionResult(
            status="invalid",
            error_code="invalid_agent_response",
            error_message=str(exc)[:500],
            agent_provider=getattr(exc, "provider", None),
            agent_model=getattr(exc, "model", None),
            agent_provider_fallback_reason=getattr(exc, "provider_fallback_reason", None),
        )
    except Exception as exc:  # noqa: BLE001 - explicit application error result
        return UniverseCompositionResult(
            status="error",
            error_code=str(getattr(exc, "code", "") or exc.__class__.__name__),
            error_message=str(exc)[:500],
            agent_provider=getattr(exc, "provider", None),
            agent_model=getattr(exc, "model", None),
            agent_provider_fallback_reason=getattr(exc, "provider_fallback_reason", None),
        )

    if not isinstance(decision, UniverseAgentDecision):
        return UniverseCompositionResult(
            status="invalid",
            validation_errors=("invalid_agent_decision_type",),
        )
    validation_errors = validate_universe_decision(request, decision)
    if validation_errors:
        return UniverseCompositionResult(
            status="invalid",
            validation_errors=validation_errors,
        )
    return UniverseCompositionResult(
        status="success",
        decision=decision,
        agent_provider=decision.provider,
        agent_model=decision.model,
        agent_provider_fallback_reason=decision.provider_fallback_reason,
    )


def _unique_symbols(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))
