"""Pure validation of global-learning citations returned by the trading LLM."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from trader.agent.protocol.types import Decision


MAX_APPLIED_LEARNING_IDS = 3


def allowed_global_learning_ids(shared_context: Mapping[str, object] | None) -> frozenset[str]:
    """Extract the stable rule ids exposed in ``context.learnings.global``.

    The LLM may only cite rules that were actually present in its current prompt.
    Malformed, legacy consolidated payloads simply expose no citeable rule rather
    than making a decision fail.
    """

    if not isinstance(shared_context, Mapping):
        return frozenset()
    learnings = shared_context.get("learnings")
    if not isinstance(learnings, Mapping):
        return frozenset()
    global_rules = learnings.get("global")
    if not isinstance(global_rules, list):
        return frozenset()
    return frozenset(
        rule_id
        for item in global_rules
        if isinstance(item, Mapping)
        and isinstance((rule_id := item.get("rule_id")), str)
        and (rule_id := rule_id.strip())
    )


def normalize_applied_learning_ids(
    raw_ids: object,
    *,
    allowed_ids: Iterable[str],
    limit: int = MAX_APPLIED_LEARNING_IDS,
) -> list[str]:
    """Keep ordered, unique citations that were available in the prompt."""

    if not isinstance(raw_ids, list) or limit <= 0:
        return []
    allowed = {str(rule_id).strip() for rule_id in allowed_ids if str(rule_id).strip()}
    selected: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        if not isinstance(raw_id, str):
            continue
        rule_id = raw_id.strip()
        if not rule_id or rule_id in seen or rule_id not in allowed:
            continue
        seen.add(rule_id)
        selected.append(rule_id)
        if len(selected) >= limit:
            break
    return selected


def filter_applied_learning_ids(
    decision: Decision,
    *,
    shared_context: Mapping[str, object] | None,
) -> Decision:
    """Return a decision with only globally exposed learning citations."""

    valid_ids = normalize_applied_learning_ids(
        decision.applied_learning_ids,
        allowed_ids=allowed_global_learning_ids(shared_context),
    )
    if valid_ids == decision.applied_learning_ids:
        return decision
    return replace(decision, applied_learning_ids=valid_ids)


__all__ = [
    "MAX_APPLIED_LEARNING_IDS",
    "allowed_global_learning_ids",
    "filter_applied_learning_ids",
    "normalize_applied_learning_ids",
]
