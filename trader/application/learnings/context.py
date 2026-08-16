"""Inbound: trader-prompt learnings slice.

``build_context_learnings`` fills ``context.learnings``. Output is citeable
``global`` rules plus optional ``guardrails``. Raw notes and ``by_symbol``
are never injected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trader.domain.learnings.consolidation import empty_consolidated, normalize_consolidated
from trader.domain.learnings.scoring import CITATION_UTILITY_UNKNOWN, citation_utility

__all__ = ["build_context_learnings", "project_global_rules"]


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def project_global_rules(
    consolidated: dict,
    *,
    rule_scores: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict]:
    """Citeable view of ``consolidated["global"]``.

    Inputs: roster payload; optional ``rule_scores``
    ``{rule_id: {q_value, q_updates}}``.
    Output: ``[{rule_id, note, robustness, citation_utility?}, ...]``.
    ``citation_utility`` is omitted while MemRL is still ``unknown``.
    ``robustness`` (FLAIR of sourced evidence) is never overwritten.

    Never emitted: ``by_symbol``, raw notes, evidence, watermark.
    Invalid roster → ``[]``.
    """

    normalized = normalize_consolidated(consolidated, watermark=consolidated.get("watermark"))
    if normalized is None:
        return []
    scores = rule_scores if isinstance(rule_scores, Mapping) else {}
    projected: list[dict] = []
    for rule in normalized["global"]:
        rule_id = str(rule["rule_id"])
        entry = {
            "rule_id": rule_id,
            "note": rule["note"],
            "robustness": rule["robustness"],
        }
        score = scores.get(rule_id)
        if isinstance(score, Mapping):
            utility = citation_utility(
                q_value=float(score.get("q_value") or 0.0),
                q_updates=_nonnegative_int(score.get("q_updates")),
            )
            if utility != CITATION_UTILITY_UNKNOWN:
                entry["citation_utility"] = utility
        projected.append(entry)
    return projected


def build_context_learnings(
    consolidated: dict,
    *,
    raw_recent: list[dict],
    guardrails: list[dict] | None = None,
    rule_scores: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict:
    """Inbound contract for ``context.learnings``.

    Inputs:
        consolidated — durable roster.
        raw_recent — accepted, discarded; never prompt-facing.
        guardrails — human invariants; copied under their own key when present.
        rule_scores — optional MemRL per ``rule_id``.

    Output: ``{"global": [...], "guardrails": [...]?}``.
    Never injected: raw notes, ``by_symbol``, evidence, watermark.
    """
    del raw_recent  # accepted, never prompt-facing
    normalized = normalize_consolidated(consolidated, watermark=consolidated.get("watermark"))
    context: dict = {
        "global": project_global_rules(
            normalized or empty_consolidated(),
            rule_scores=rule_scores,
        )
    }
    if guardrails:
        context["guardrails"] = guardrails
    return context
