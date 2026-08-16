"""Inbound use case: decide when to consolidate and accept a proposed roster.

Depends only on domain policy and outbound ports. Prompt, transport and JSON
recovery stay behind ``LearningComposer``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from trader.application.learnings.protocols import (
    ConsolidatedLearningsPort,
    ConsolidationStatusPort,
    CurationPort,
    LearningComposer,
    RawLearningsPort,
)
from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION
from trader.domain.learnings.consolidation import (
    DEFAULT_MAX_GLOBAL,
    current_note_ids,
    curation_due_reason,
    empty_consolidated,
    new_rule_id,
    normalize_consolidated,
    normalize_rule_note,
    restore_omitted_current_rules,
)
from trader.domain.learnings.curation import build_curation_candidates
from trader.domain.learnings.selection import parse_ts, select_new_raw

DEFAULT_CONSOLIDATION_THRESHOLD = 50
DEFAULT_CONSOLIDATOR_TIMEOUT_S = 240

_VERDICT_COUNTS = {"WIN": "wins", "LOSS": "losses", "NEUTRAL": "neutrals"}
_VERDICT_REWARD = {"WIN": 1.0, "LOSS": -1.0, "NEUTRAL": 0.0}

__all__ = [
    "DEFAULT_CONSOLIDATION_THRESHOLD",
    "DEFAULT_CONSOLIDATOR_TIMEOUT_S",
    "consolidate_payload",
    "finalize_consolidated_payload",
    "maybe_consolidate",
]


def _max_ts(rows: list[dict]) -> str | None:
    parsed = [parse_ts(row.get("ts")) for row in rows]
    valid = [item for item in parsed if item is not None]
    if not valid:
        return None
    return max(valid).isoformat()


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _failure_payload(
    code: str,
    message: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    output: str | None = None,
) -> dict:
    payload = {"error_code": code, "error_message": message[:500]}
    if provider is not None:
        payload["provider"] = provider
    if model is not None:
        payload["model"] = model
    if output is not None:
        payload["output_preview"] = output[:500]
        payload["output_tail"] = output[-500:]
        payload["output_length"] = len(output)
    return payload


def _failure_result(error: dict, *, new_raw_count: int) -> dict:
    result = {
        "triggered": True,
        "new_raw_count": new_raw_count,
        "written": False,
        "error_code": error["error_code"],
        "error_message": error["error_message"],
    }
    if error.get("error_code") == "invalid_json":
        for field in ("provider", "model", "output_preview", "output_tail", "output_length"):
            if field in error:
                result[field] = error[field]
    return result


def _candidate_evidence_summary(evidence_rows: list[dict]) -> dict:
    summary = {
        "evaluated": 0,
        "pending": 0,
        "wins": 0,
        "losses": 0,
        "neutrals": 0,
        "mean_reward": None,
    }
    rewards: list[float] = []
    for row in evidence_rows:
        feedback = row.get("feedback") if isinstance(row.get("feedback"), dict) else {}
        verdict = str(feedback.get("verdict") or "UNKNOWN").upper()
        count_key = _VERDICT_COUNTS.get(verdict)
        if count_key is None:
            summary["pending"] += 1
        else:
            summary["evaluated"] += 1
            summary[count_key] += 1
        reward = _as_float(feedback.get("reward"))
        if reward is None:
            reward = _VERDICT_REWARD.get(verdict)
        if reward is not None:
            rewards.append(reward)
    if rewards:
        summary["mean_reward"] = sum(rewards) / len(rewards)
    return summary


def _resolved_robustness(requested: object, summary: dict) -> str:
    desired = str(requested or "").strip().lower()
    if desired not in {"pending", "low", "high"}:
        desired = "pending" if summary["pending"] else "low"
    high_allowed = (
        summary["evaluated"] >= 3
        and summary["wins"] > summary["losses"]
        and summary["mean_reward"] is not None
        and float(summary["mean_reward"]) > 0.0
    )
    if desired == "high" and not high_allowed:
        return "pending" if summary["pending"] else "low"
    return desired


def _assigned_rule_id(
    raw_rule: dict,
    *,
    note: str,
    existing_ids: set[str],
    current_by_note: dict[str, str],
    used_ids: set[str],
) -> tuple[str | None, dict | None]:
    supplied_id = str(raw_rule.get("rule_id") or "").strip()
    if supplied_id and supplied_id not in existing_ids:
        return None, _failure_payload(
            "invalid_payload",
            f"unknown rule_id {supplied_id!r}; new rules must omit rule_id",
        )
    inherited_id = current_by_note.get(normalize_rule_note(note), "")
    if not supplied_id and inherited_id and inherited_id not in used_ids:
        rule_id = inherited_id
    else:
        rule_id = supplied_id or new_rule_id(note, used_ids=used_ids | existing_ids)
    if rule_id in used_ids:
        return None, _failure_payload("invalid_payload", f"duplicate rule_id {rule_id!r}")
    return rule_id, None


def _collect_evidence_ids(
    raw_rule: dict,
    *,
    require_evidence: bool,
    allowed_evidence: dict[str, dict],
) -> tuple[list[str] | None, dict | None]:
    raw_evidence = raw_rule.get("evidence_note_ids")
    if raw_evidence is None and not require_evidence:
        return [], None
    if not isinstance(raw_evidence, list):
        return None, _failure_payload("invalid_payload", "evidence_note_ids must be a list")
    evidence_ids: list[str] = []
    for raw_id in raw_evidence:
        evidence_id = str(raw_id).strip()
        if evidence_id and evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
    if require_evidence and not evidence_ids:
        return None, _failure_payload("invalid_payload", "each global rule needs source evidence")
    unknown_evidence = [evidence_id for evidence_id in evidence_ids if evidence_id not in allowed_evidence]
    if unknown_evidence:
        return None, _failure_payload(
            "invalid_payload",
            f"evidence ids absent from curation candidates: {unknown_evidence!r}",
        )
    return evidence_ids, None


def finalize_consolidated_payload(
    payload: dict,
    *,
    current: dict,
    candidates: list[dict],
    require_evidence: bool,
    now: datetime | None = None,
) -> tuple[dict | None, dict | None]:
    """Attach machine-owned ids, evidence and keep/restore. Reject unknown ids."""

    raw_global = payload.get("global")
    if not isinstance(raw_global, list):
        return None, _failure_payload("invalid_payload", "global must be a list")

    clock = now or datetime.now(timezone.utc)
    current_global = [rule for rule in current.get("global", []) if isinstance(rule, dict) and rule.get("rule_id")]
    normalized_current = normalize_consolidated(current, watermark=current.get("watermark")) or empty_consolidated()
    existing_ids = {str(rule["rule_id"]) for rule in normalized_current["global"] if rule.get("rule_id")}
    current_by_note = current_note_ids(normalized_current["global"])
    allowed_evidence = {str(candidate["id"]): candidate for candidate in candidates}
    used_ids: set[str] = set()
    finalized: list[dict] = []

    for raw_rule in raw_global[:DEFAULT_MAX_GLOBAL]:
        if not isinstance(raw_rule, dict):
            return None, _failure_payload("invalid_payload", "global entries must be objects")
        note = str(raw_rule.get("note") or "").strip()
        if not note:
            return None, _failure_payload("invalid_payload", "global rule note is required")

        rule_id, error = _assigned_rule_id(
            raw_rule,
            note=note,
            existing_ids=existing_ids,
            current_by_note=current_by_note,
            used_ids=used_ids,
        )
        if error is not None or rule_id is None:
            return None, error

        evidence_ids, error = _collect_evidence_ids(
            raw_rule,
            require_evidence=require_evidence,
            allowed_evidence=allowed_evidence,
        )
        if error is not None or evidence_ids is None:
            return None, error

        summary = _candidate_evidence_summary([allowed_evidence[evidence_id] for evidence_id in evidence_ids])
        finalized.append(
            {
                "rule_id": rule_id,
                "note": note,
                "robustness": _resolved_robustness(raw_rule.get("robustness"), summary),
                "evidence_note_ids": evidence_ids,
                "evidence_summary": summary,
            }
        )
        used_ids.add(rule_id)

    restored = restore_omitted_current_rules(
        finalized,
        current_global,
        existing_ids=existing_ids,
        now=clock,
    )
    return {
        "outcome_semantics_version": BENCHMARK_SEMANTICS_VERSION,
        "global": restored,
        "by_symbol": {},
    }, None


def _accept_proposal(
    current: dict,
    new_raw: list[dict],
    *,
    composer: LearningComposer,
    candidate_rows: list[dict] | None,
    require_evidence: bool,
    attribution: dict | None,
    meta_performance: dict | None,
    timeout_s: int,
    max_attempts: int,
    now: datetime | None,
) -> tuple[dict | None, dict | None]:
    """Retry compose unless the composer marks the error non-retryable."""

    candidates = build_curation_candidates(new_raw, candidate_rows=candidate_rows)
    last_error: dict | None = None
    for _attempt in range(max(1, int(max_attempts))):
        raw, error = composer.compose(
            current,
            candidates,
            attribution=attribution,
            meta_performance=meta_performance,
            timeout_s=timeout_s,
            max_attempts=1,
        )
        if raw is None:
            last_error = error
            if error is not None and error.get("retryable") is False:
                return None, error
            continue
        finalized, validation_error = finalize_consolidated_payload(
            raw,
            current=current,
            candidates=candidates,
            require_evidence=require_evidence,
            now=now,
        )
        if finalized is not None:
            normalized = normalize_consolidated(finalized, watermark=current.get("watermark"))
            if normalized is not None:
                return normalized, None
        last_error = validation_error or _failure_payload("invalid_payload", "consolidateur returned invalid payload")
    return None, last_error


def consolidate_payload(
    current: dict,
    new_raw: list[dict],
    *,
    composer: LearningComposer,
    candidate_rows: list[dict] | None = None,
    require_evidence: bool | None = None,
    attribution: dict | None = None,
    meta_performance: dict | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> dict | None:
    consolidated, _error = _accept_proposal(
        current,
        new_raw,
        composer=composer,
        candidate_rows=candidate_rows,
        require_evidence=(candidate_rows is not None if require_evidence is None else require_evidence),
        attribution=attribution,
        meta_performance=meta_performance,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        now=now,
    )
    return consolidated


def _previous_failure_backoff(
    last_failure: object,
    *,
    current_watermark: object,
    requested_model: str | None,
    raw_rows: list[dict],
    threshold: int,
    new_raw_count: int,
) -> dict | None:
    if not isinstance(last_failure, dict):
        return None
    last_model = last_failure.get("requested_model", last_failure.get("model"))
    if last_failure.get("consolidated_watermark") != current_watermark:
        return None
    if last_model not in (None, requested_model):
        return None
    raw_since_failure = select_new_raw(raw_rows, watermark=last_failure.get("raw_watermark"))
    if len(raw_since_failure) >= threshold:
        return None
    return {
        "triggered": False,
        "new_raw_count": new_raw_count,
        "skipped": True,
        "reason": "previous_failure_backoff",
        "new_raw_since_failure": len(raw_since_failure),
        "retry_after_new_raw": threshold,
        "last_error_code": last_failure.get("error_code"),
    }


def _recorded_failure(
    status: ConsolidationStatusPort,
    *,
    current: dict,
    new_raw: list[dict],
    error: dict,
    requested_model: str | None,
) -> dict:
    error = {**error, "requested_model": requested_model}
    status.write_failure(
        consolidated_watermark=current.get("watermark"),
        raw_watermark=_max_ts(new_raw),
        new_raw_count=len(new_raw),
        error=error,
    )
    return _failure_result(error, new_raw_count=len(new_raw))


def _curation_ack(
    curation: CurationPort,
    *,
    consolidated_payload: dict,
    new_raw: list[dict],
    resolved_candidate_rows: list[dict] | None,
    pending_snapshot: list[dict] | None,
    watermark: object,
) -> dict:
    extras: dict[str, Any] = {}
    sync = curation.sync_rules([str(rule["rule_id"]) for rule in consolidated_payload.get("global", [])])
    candidates = build_curation_candidates(new_raw, candidate_rows=resolved_candidate_rows)
    extras["curated_candidate_count"] = curation.acknowledge(
        candidates=list(pending_snapshot) if pending_snapshot else candidates,
        source_rows=pending_snapshot if pending_snapshot else resolved_candidate_rows,
        watermark=watermark,
    )
    if sync is not None:
        extras["global_rule_sync"] = sync
    return extras


def maybe_consolidate(
    *,
    raw: RawLearningsPort,
    consolidated: ConsolidatedLearningsPort,
    status: ConsolidationStatusPort,
    composer: LearningComposer,
    curation: CurationPort | None = None,
    threshold: int = DEFAULT_CONSOLIDATION_THRESHOLD,
    candidate_rows: list[dict] | None = None,
    attribution: dict | None = None,
    meta_performance: dict | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
    now: datetime | None = None,
    requested_model: str | None = None,
) -> dict:
    """Run one consolidation cycle when the raw or feedback threshold is due."""

    current = consolidated.read()
    current_for_curation = curation.attach_memrl(current) if curation is not None else current
    raw_rows = raw.all()
    new_raw = select_new_raw(raw_rows, watermark=current.get("watermark"))
    threshold = max(1, threshold)
    clock = now or datetime.now(timezone.utc)
    counts = curation.counts() if curation is not None else None
    due = curation_due_reason(
        dict(counts) if isinstance(counts, Mapping) else None,
        threshold=threshold,
        now=clock,
    )
    if len(new_raw) < threshold and due is None:
        return {"triggered": False, "new_raw_count": len(new_raw)}

    backoff = _previous_failure_backoff(
        status.read().get("last_failure"),
        current_watermark=current.get("watermark"),
        requested_model=requested_model,
        raw_rows=raw_rows,
        threshold=threshold,
        new_raw_count=len(new_raw),
    )
    if backoff is not None:
        return backoff

    pending_snapshot = curation.snapshot_pending() if curation is not None else None
    resolved_candidate_rows = candidate_rows
    if resolved_candidate_rows is None and curation is not None:
        try:
            resolved_candidate_rows = curation.select_candidates(
                current=current_for_curation,
                new_raw=new_raw,
            )
        except Exception as exc:
            return _recorded_failure(
                status,
                current=current,
                new_raw=new_raw,
                error=_failure_payload("curation_provider", str(exc)),
                requested_model=requested_model,
            )

    consolidated_payload, error = _accept_proposal(
        current_for_curation,
        new_raw,
        composer=composer,
        candidate_rows=resolved_candidate_rows,
        require_evidence=resolved_candidate_rows is not None,
        attribution=attribution,
        meta_performance=meta_performance,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        now=clock,
    )
    if consolidated_payload is None:
        return _recorded_failure(
            status,
            current=current,
            new_raw=new_raw,
            error=error or _failure_payload("unknown", "unknown consolidation failure"),
            requested_model=requested_model,
        )

    watermark = _max_ts(new_raw) or current.get("watermark")
    consolidated.write(consolidated_payload, watermark=watermark)
    status.clear()
    result: dict[str, Any] = {
        "triggered": True,
        "new_raw_count": len(new_raw),
        "written": True,
        **({"curation_due": due} if due else {}),
    }
    if curation is not None:
        result.update(
            _curation_ack(
                curation,
                consolidated_payload=consolidated_payload,
                new_raw=new_raw,
                resolved_candidate_rows=resolved_candidate_rows,
                pending_snapshot=pending_snapshot,
                watermark=watermark,
            )
        )
    return result
