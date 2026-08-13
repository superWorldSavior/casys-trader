"""Consolidateur machine-owned des learnings runtime.

Le store brut `learnings.jsonl` reste un buffer jetable. Ce module synthétise
périodiquement ces notes vers `learnings_consolidated.json`, séparé de la mémoire
humaine `mandate/memory.md`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Any

from trader.agent import llm
from trader.agent.learnings.consolidation_prompt import build_consolidation_prompt
from trader.agent.learnings.consolidation_stores import (
    DEFAULT_MAX_BY_SYMBOL,
    DEFAULT_MAX_GLOBAL,
    ConsolidatedLearningsStore,
    ConsolidationStatusStore,
    empty_consolidated,
    normalize_consolidated,
    stable_rule_id,
)
from trader.agent.learnings.selection import _parse_ts, pending_raw_count, select_new_raw
from trader.agent.learnings.raw_store import RawLearningsStore
from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION

log = logging.getLogger(__name__)

DEFAULT_RAW_MAX_ENTRIES = 200
DEFAULT_CONSOLIDATION_THRESHOLD = 50
DEFAULT_CONSOLIDATOR_PROVIDER = "consolidator"
DEFAULT_CONSOLIDATOR_ACPX_AGENT = "codex"
DEFAULT_CONSOLIDATOR_MODEL = "gpt-5.6-sol"
DEFAULT_CONSOLIDATOR_TIMEOUT_S = 240
DEFAULT_MAX_RECENT_CANDIDATES = 50
DEFAULT_MAX_CONFIRMATION_CANDIDATES = 15
DEFAULT_MAX_COUNTEREXAMPLE_CANDIDATES = 15
DEFAULT_MAX_CURATION_CANDIDATES = (
    DEFAULT_MAX_RECENT_CANDIDATES
    + DEFAULT_MAX_CONFIRMATION_CANDIDATES
    + DEFAULT_MAX_COUNTEREXAMPLE_CANDIDATES
)
DEFAULT_FEEDBACK_CONSOLIDATION_THRESHOLD = 10
DEFAULT_CURATION_CATCH_UP_AGE = timedelta(days=1)
# A soft cap prevents one very chatty symbol from consuming the whole curation
# prompt.  It is relaxed only if no other candidates remain, so small universes
# still make progress.
DEFAULT_SOFT_MAX_CANDIDATES_PER_SYMBOL = 12

__all__ = [
    "DEFAULT_CONSOLIDATION_THRESHOLD",
    "DEFAULT_CONSOLIDATOR_ACPX_AGENT",
    "DEFAULT_CONSOLIDATOR_MODEL",
    "DEFAULT_CONSOLIDATOR_PROVIDER",
    "DEFAULT_CONSOLIDATOR_TIMEOUT_S",
    "DEFAULT_MAX_CONFIRMATION_CANDIDATES",
    "DEFAULT_MAX_COUNTEREXAMPLE_CANDIDATES",
    "DEFAULT_MAX_CURATION_CANDIDATES",
    "DEFAULT_MAX_RECENT_CANDIDATES",
    "DEFAULT_MAX_BY_SYMBOL",
    "DEFAULT_MAX_GLOBAL",
    "DEFAULT_RAW_MAX_ENTRIES",
    "ConsolidatedLearningsStore",
    "ConsolidationStatusStore",
    "build_consolidation_prompt",
    "build_curation_candidates",
    "build_consolidator_router_from_env",
    "build_context_learnings",
    "consolidate_payload",
    "empty_consolidated",
    "load_guardrails",
    "maybe_consolidate",
    "normalize_consolidated",
    "project_global_rules",
]


def _max_ts(rows: list[dict]) -> str | None:
    parsed = [_parse_ts(row.get("ts")) for row in rows]
    valid = [item for item in parsed if item is not None]
    if not valid:
        return None
    return max(valid).isoformat()


def _has_consolidated(payload: dict) -> bool:
    return bool(payload.get("global"))


_GUARDRAILS_CACHE: dict[str, tuple[float, list[dict]]] = {}


def load_guardrails(path: Path) -> list[dict]:
    """Garde-fous invariants, écrits par l'humain (D6 : séparés des patterns machine).

    Cache par mtime : le fichier est statique en exécution normale, inutile de le
    relire à chaque cycle ; une édition humaine est prise en compte au cycle suivant.
    """
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _GUARDRAILS_CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    guardrails = [entry for entry in payload if isinstance(entry, dict) and entry.get("note")]
    _GUARDRAILS_CACHE[str(path)] = (mtime, guardrails)
    return guardrails


def build_context_learnings(
    consolidated: dict,
    *,
    raw_recent: list[dict],
    guardrails: list[dict] | None = None,
) -> dict:
    """Contexte learnings injecté à l'agent (D6 du registre).

    `guardrails` = invariants humains, toujours présents et nommés à part.
    Les bruts, `by_symbol` legacy et autres expériences historiques ne sont
    jamais injectés : la continuité symbole est déjà fournie par
    ``last_llm_review``/``recent_decisions`` et l'exploration historique passe
    seulement par l'outil ``recall_learnings``.
    """
    del raw_recent  # compatibility parameter; intentionally never prompt-facing
    normalized = normalize_consolidated(consolidated, watermark=consolidated.get("watermark"))
    context: dict = {"global": project_global_rules(normalized or empty_consolidated())}
    if guardrails:
        context["guardrails"] = guardrails
    return context


def project_global_rules(consolidated: dict) -> list[dict]:
    """Expose only the compact, citeable global-rule contract to a trader."""

    normalized = normalize_consolidated(consolidated, watermark=consolidated.get("watermark"))
    if normalized is None:
        return []
    return [
        {
            "rule_id": rule["rule_id"],
            "note": rule["note"],
            "robustness": rule["robustness"],
        }
        for rule in normalized["global"]
    ]


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _candidate_id(row: dict, *, index: int) -> str:
    """Derive a stable source-note id for legacy JSONL rows without SQLite ids."""

    raw_id = row.get("id")
    if raw_id is None:
        raw_id = row.get("note_id")
    if raw_id is not None and str(raw_id).strip():
        return str(raw_id).strip()
    decision_id = str(row.get("decision_id") or "").strip()
    if decision_id:
        return decision_id
    return "legacy:{}:{}:{}".format(
        row.get("ts") or "unknown-ts",
        row.get("symbol") or "unknown-symbol",
        index,
    )


def _candidate_identity(row: dict, *, index: int) -> str:
    """Deduplicate the same note when it appears as raw and SQLite history."""

    decision_id = str(row.get("decision_id") or "").strip()
    if decision_id:
        return f"decision:{decision_id}"
    return f"candidate:{_candidate_id(row, index=index)}"


def _candidate_status(row: dict) -> str:
    verdict = str(row.get("verdict") or "").upper()
    return "evaluated" if verdict in {"WIN", "LOSS", "NEUTRAL"} else "pending"


def _candidate_from_row(row: dict, *, index: int) -> dict | None:
    note = str(row.get("note") or row.get("text") or "").strip()
    if not note:
        return None
    verdict = str(row.get("verdict") or "UNKNOWN").upper()
    if verdict not in {"WIN", "LOSS", "NEUTRAL", "UNKNOWN"}:
        verdict = "UNKNOWN"
    return {
        "id": _candidate_id(row, index=index),
        "identity": _candidate_identity(row, index=index),
        "ts": str(row.get("ts") or ""),
        "symbol": str(row.get("symbol") or ""),
        "family": row.get("family"),
        "decision_id": row.get("decision_id"),
        "action": row.get("action"),
        "intent": row.get("intent"),
        "executed": row.get("executed"),
        "note": note,
        "feedback": {
            "status": _candidate_status(row),
            "verdict": verdict,
            "reward": _as_float(row.get("reward")),
            "forward_return": _as_float(row.get("forward_return")),
            "outcome_score": _as_float(row.get("outcome_score")),
            "q_value": _as_float(row.get("q_value")),
            "q_updates": _nonnegative_int(row.get("q_updates")),
        },
    }


def _candidate_time(candidate: dict) -> datetime:
    return _parse_ts(candidate.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)


def _candidate_quality(candidate: dict) -> float:
    feedback = candidate["feedback"]
    flair = float(feedback.get("outcome_score") or 0.0)
    q_value = float(feedback.get("q_value") or 0.0)
    q_updates = _nonnegative_int(feedback.get("q_updates"))
    q_confidence = q_updates / (q_updates + 5.0)
    return flair + 0.2 * q_value * q_confidence


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _append_with_soft_symbol_cap(
    selected: list[dict],
    rows: Iterable[dict],
    *,
    limit: int,
    seen: set[str],
    symbol_counts: dict[str, int],
) -> None:
    """Append a ranked category with diversity first, then a safe fill pass."""

    candidates = list(rows)
    deferred: list[dict] = []
    for candidate in candidates:
        if len(selected) >= limit:
            return
        identity = candidate["identity"]
        if identity in seen:
            continue
        symbol = candidate["symbol"]
        if symbol and symbol_counts.get(symbol, 0) >= DEFAULT_SOFT_MAX_CANDIDATES_PER_SYMBOL:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        seen.add(identity)
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
    # If the universe truly has no diversity left, do not starve the
    # consolidation just to satisfy the soft cap.
    for candidate in deferred:
        if len(selected) >= limit:
            return
        identity = candidate["identity"]
        if identity in seen:
            continue
        selected.append(candidate)
        seen.add(identity)
        symbol = candidate["symbol"]
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1


def build_curation_candidates(
    new_raw: list[dict],
    *,
    candidate_rows: list[dict] | None = None,
) -> list[dict]:
    """Return the bounded 50 recent + 15 confirm + 15 counterexample pool.

    ``candidate_rows`` may contain FLAIR/MemRL-enriched SQLite rows supplied by
    a runtime provider.  The new raw rows are always included as provisional
    candidates, so a consolidation can still run during a DB rebuild.
    """

    recent_candidates = [
        candidate
        for index, row in enumerate(new_raw)
        if (candidate := _candidate_from_row(row, index=index)) is not None
    ]
    history_candidates = [
        candidate
        for index, row in enumerate(candidate_rows or [])
        if (candidate := _candidate_from_row(row, index=index + len(new_raw))) is not None
    ]
    for candidate in recent_candidates:
        candidate["_enriched"] = False
    for candidate in history_candidates:
        candidate["_enriched"] = True

    selected: list[dict] = []
    seen: set[str] = set()
    symbol_counts: dict[str, int] = {}
    _append_with_soft_symbol_cap(
        selected,
        sorted(
            recent_candidates + history_candidates,
            key=lambda candidate: (_candidate_time(candidate), bool(candidate["_enriched"])),
            reverse=True,
        ),
        limit=DEFAULT_MAX_RECENT_CANDIDATES,
        seen=seen,
        symbol_counts=symbol_counts,
    )

    all_candidates = history_candidates + recent_candidates
    confirmations = sorted(
        (
            candidate
            for candidate in all_candidates
            if candidate["feedback"]["verdict"] == "WIN" or _candidate_quality(candidate) > 0.0
        ),
        key=lambda candidate: (_candidate_quality(candidate), _candidate_time(candidate)),
        reverse=True,
    )
    _append_with_soft_symbol_cap(
        selected,
        confirmations,
        limit=DEFAULT_MAX_RECENT_CANDIDATES + DEFAULT_MAX_CONFIRMATION_CANDIDATES,
        seen=seen,
        symbol_counts=symbol_counts,
    )

    counterexamples = sorted(
        (
            candidate
            for candidate in all_candidates
            if candidate["feedback"]["verdict"] == "LOSS" or _candidate_quality(candidate) < 0.0
        ),
        key=lambda candidate: (_candidate_quality(candidate), _candidate_time(candidate)),
    )
    _append_with_soft_symbol_cap(
        selected,
        counterexamples,
        limit=DEFAULT_MAX_CURATION_CANDIDATES,
        seen=seen,
        symbol_counts=symbol_counts,
    )

    # ``identity`` is an implementation-only dedupe aid and must not leak to
    # the LLM contract.
    return [
        {key: value for key, value in candidate.items() if key not in {"identity", "_enriched"}}
        for candidate in selected
    ]


def _clean_optional(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _resolved_consolidator_model(model: str | None) -> str:
    return _clean_optional(model) or os.environ.get("TRADER_CONSOLIDATOR_MODEL") or DEFAULT_CONSOLIDATOR_MODEL


def build_consolidator_router_from_env(
    *,
    env_path: str | Path | None = llm.DEFAULT_ENV_PATH,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
) -> llm.LlmRouter:
    llm.load_dotenv(env_path)
    resolved_bin = _clean_optional(acpx_bin) or os.environ.get("TRADER_CONSOLIDATOR_ACPX_BIN") or "acpx"
    resolved_agent = (
        _clean_optional(acpx_agent)
        or os.environ.get("TRADER_CONSOLIDATOR_ACPX_AGENT")
        or DEFAULT_CONSOLIDATOR_ACPX_AGENT
    )
    resolved_model = _resolved_consolidator_model(model)
    return llm.build_default_router_from_env(
        env_path=None,
        acpx_bin=resolved_bin,
        spark_model=resolved_model,
        acpx_provider=DEFAULT_CONSOLIDATOR_PROVIDER,
        acpx_agent=resolved_agent,
    )


def _default_status_store(consolidated_store: ConsolidatedLearningsStore) -> ConsolidationStatusStore:
    return ConsolidationStatusStore(consolidated_store.path.with_name("learnings_consolidation_status.json"))


def _failure_from_llm(completion: llm.LlmFailure) -> dict:
    return {
        "error_code": completion.code,
        "error_message": completion.message[:500],
        "provider": completion.provider,
        "model": completion.model,
    }


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


def _looks_like_consolidated_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and "global" in payload


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
        if verdict in {"WIN", "LOSS", "NEUTRAL"}:
            summary["evaluated"] += 1
            if verdict == "WIN":
                summary["wins"] += 1
            elif verdict == "LOSS":
                summary["losses"] += 1
            else:
                summary["neutrals"] += 1
        else:
            summary["pending"] += 1
        reward = _as_float(feedback.get("reward"))
        if reward is None:
            reward = {
                "WIN": 1.0,
                "LOSS": -1.0,
                "NEUTRAL": 0.0,
            }.get(verdict)
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


def _new_rule_id(note: str, *, used_ids: set[str]) -> str:
    candidate = stable_rule_id(note)
    if candidate not in used_ids:
        return candidate
    # Same text already maps to the same semantic rule.  A suffixed id keeps
    # distinct rules collision-safe without involving the LLM.
    suffix = 2
    while f"{candidate}_{suffix}" in used_ids:
        suffix += 1
    return f"{candidate}_{suffix}"


def _finalize_consolidated_payload(
    payload: dict,
    *,
    current: dict,
    candidates: list[dict],
    require_evidence: bool,
) -> tuple[dict | None, dict | None]:
    """Validate model output and attach all machine-owned rule metadata."""

    raw_global = payload.get("global")
    if not isinstance(raw_global, list):
        return None, _failure_payload("invalid_payload", "global must be a list")

    normalized_current = normalize_consolidated(
        current, watermark=current.get("watermark")
    ) or empty_consolidated()
    existing_ids = {
        str(rule["rule_id"])
        for rule in normalized_current["global"]
        if rule.get("rule_id")
    }
    allowed_evidence = {str(candidate["id"]): candidate for candidate in candidates}
    used_ids: set[str] = set()
    finalized: list[dict] = []

    for raw_rule in raw_global[:DEFAULT_MAX_GLOBAL]:
        if not isinstance(raw_rule, dict):
            return None, _failure_payload("invalid_payload", "global entries must be objects")
        note = str(raw_rule.get("note") or "").strip()
        if not note:
            return None, _failure_payload("invalid_payload", "global rule note is required")

        supplied_id = str(raw_rule.get("rule_id") or "").strip()
        if supplied_id and supplied_id not in existing_ids:
            return None, _failure_payload(
                "invalid_payload",
                f"unknown rule_id {supplied_id!r}; new rules must omit rule_id",
            )
        rule_id = supplied_id or _new_rule_id(note, used_ids=used_ids | existing_ids)
        if rule_id in used_ids:
            return None, _failure_payload("invalid_payload", f"duplicate rule_id {rule_id!r}")

        raw_evidence = raw_rule.get("evidence_note_ids")
        if raw_evidence is None and not require_evidence:
            evidence_ids: list[str] = []
        elif not isinstance(raw_evidence, list):
            return None, _failure_payload("invalid_payload", "evidence_note_ids must be a list")
        else:
            evidence_ids = []
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

    return {
        "outcome_semantics_version": BENCHMARK_SEMANTICS_VERSION,
        "global": finalized,
        "by_symbol": {},
    }, None


def _call_curation_provider(
    provider: object,
    *,
    current: dict,
    new_raw: list[dict],
) -> list[dict]:
    """Read optional enrichened candidates without coupling this module to SQLite.

    The supported provider surface is intentionally small:
    ``select_candidates(current=..., new_raw=...)``, the concrete
    ``LearningsStore.select_curation_candidates()``, or a callable with the
    first signature.  It makes the consolidator usable in tests and in a
    derived-store rebuild where the RAG database is absent.
    """

    selector = getattr(provider, "select_candidates", None) or getattr(
        provider, "select_curation_candidates", None
    )
    if not callable(selector) and callable(provider):
        selector = provider
    if not callable(selector):
        raise TypeError("curation_provider must expose select_candidates(...) or be callable")
    try:
        rows = selector(current=current, new_raw=new_raw)
    except TypeError:
        # The concrete SQLite provider owns its own cadence/pool query and has
        # no need for the raw JSONL inputs.
        rows = selector()
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise TypeError("curation_provider must return list[dict]")
    return rows


def _curation_counts(provider: object | None) -> dict[str, object] | None:
    """Read curation work without making the optional SQLite store mandatory."""

    counter = getattr(provider, "curation_counts", None)
    if not callable(counter):
        return None
    try:
        payload = counter()
    except Exception as exc:  # pragma: no cover - fail-open maintenance seam
        log.warning("unable to read curation counts (%s)", exc)
        return None
    return payload if isinstance(payload, dict) else None


def _curation_due_reason(
    counts: dict[str, object] | None,
    *,
    threshold: int,
    now: datetime,
) -> str | None:
    if not counts:
        return None
    new_count = int(counts.get("new") or 0)
    feedback_count = int(counts.get("feedback") or 0)
    if new_count >= threshold:
        return "new_threshold"
    if feedback_count >= DEFAULT_FEEDBACK_CONSOLIDATION_THRESHOLD:
        return "feedback_threshold"
    if new_count + feedback_count <= 0:
        return None
    oldest = _parse_ts(counts.get("oldest_changed_at"))
    if oldest is not None and now - oldest >= DEFAULT_CURATION_CATCH_UP_AGE:
        return "daily_catch_up"
    return None


def _mark_candidates_curated(
    provider: object | None,
    *,
    candidates: list[dict],
    source_rows: list[dict] | None,
    watermark: str | None,
) -> int:
    """Best-effort acknowledgement after the new consolidated payload is durable."""

    if provider is None:
        return 0
    store_marker = getattr(provider, "mark_curation_candidates_curated", None)
    if callable(store_marker):
        candidate_ids = {str(candidate["id"]) for candidate in candidates}
        candidate_decision_ids = {
            str(candidate.get("decision_id") or "")
            for candidate in candidates
            if candidate.get("decision_id")
        }
        source_rows = source_rows or []
        source_subset = [
            row
            for index, row in enumerate(source_rows)
            if (
                _candidate_id(row, index=index) in candidate_ids
                or str(row.get("decision_id") or "") in candidate_decision_ids
            )
        ]
        try:
            result = store_marker(source_subset)
        except Exception as exc:  # pragma: no cover - defensive integration seam
            log.warning("unable to mark curated learning candidates (%s)", exc)
            return 0
        return int(result) if isinstance(result, int) else len(source_subset)

    marker = getattr(provider, "mark_candidates_curated", None) or getattr(provider, "mark_curated", None)
    if not callable(marker):
        return 0
    candidate_ids = [str(candidate["id"]) for candidate in candidates]
    try:
        result = marker(candidate_ids=candidate_ids, watermark=watermark)
    except TypeError:
        # A minimal provider can use the positional form; its real errors are
        # still logged below and never undo a successful consolidation write.
        try:
            result = marker(candidate_ids)
        except Exception as exc:  # pragma: no cover - defensive integration seam
            log.warning("unable to mark curated learning candidates (%s)", exc)
            return 0
    except Exception as exc:  # pragma: no cover - defensive integration seam
        log.warning("unable to mark curated learning candidates (%s)", exc)
        return 0
    return int(result) if isinstance(result, int) else len(candidate_ids)


def _sync_active_global_rules(
    provider: object | None,
    *,
    consolidated: dict,
) -> dict | None:
    """Mirror active stable rule IDs into the optional MemRL rule store."""

    if provider is None:
        return None
    sync = getattr(provider, "sync_global_rules", None)
    if not callable(sync):
        return None
    rule_ids = [str(rule["rule_id"]) for rule in consolidated.get("global", [])]
    try:
        result = sync(rule_ids)
    except Exception as exc:  # pragma: no cover - defensive integration seam
        log.warning("unable to synchronize active global rules (%s)", exc)
        return None
    return result if isinstance(result, dict) else {"active": len(rule_ids)}


def _with_global_rule_memrl(current: dict, provider: object | None) -> dict:
    """Expose shrunk rule Q only to the consolidator, never to the trader prompt."""

    reader = getattr(provider, "global_rule_scores", None)
    if not callable(reader):
        return current
    rule_ids = [
        str(rule.get("rule_id"))
        for rule in current.get("global", [])
        if isinstance(rule, dict) and rule.get("rule_id")
    ]
    if not rule_ids:
        return current
    try:
        scores = reader(rule_ids)
    except Exception as exc:  # pragma: no cover - advisory curation enrichment
        log.warning("unable to read global rule MemRL scores (%s)", exc)
        return current
    if not isinstance(scores, dict):
        return current
    rules: list[dict] = []
    for rule in current.get("global", []):
        if not isinstance(rule, dict):
            continue
        copied = dict(rule)
        score = scores.get(str(copied.get("rule_id") or ""))
        if isinstance(score, dict):
            q_value = float(score.get("q_value") or 0.0)
            q_updates = max(0, int(score.get("q_updates") or 0))
            copied["memrl"] = {
                "q_value": q_value,
                "q_updates": q_updates,
                "shrunk_q": q_value * q_updates / (q_updates + 5.0),
            }
        rules.append(copied)
    return {**current, "global": rules}


def _closing_suffix_for_truncated_json(text: str) -> str | None:
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack.pop() != pairs[char]:
                return None
    if in_string or not stack:
        return None
    return "".join("}" if char == "{" else "]" for char in reversed(stack))


def _repair_truncated_consolidated_payload(text: str) -> dict | None:
    suffix = _closing_suffix_for_truncated_json(text)
    if suffix is None:
        return None
    try:
        payload = json.loads(text + suffix)
    except json.JSONDecodeError:
        return None
    if _looks_like_consolidated_payload(payload):
        return payload
    return None


def _parse_consolidated_json_from_text(text: str) -> tuple[dict | None, json.JSONDecodeError | None]:
    """Parse le JSON consolidé depuis stdout acpx.

    `acpx --format quiet` peut concaténer plusieurs messages assistant. Si le
    premier message est un statut et le dernier est le JSON, `json.loads(stdout)`
    échoue au caractère 0. On garde donc le contrat strict, mais on récupère le
    dernier objet complet qui ressemble au payload attendu.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        first_error = exc
    else:
        if isinstance(payload, dict):
            return payload, None
        return None, None

    decoder = json.JSONDecoder()
    candidate: dict | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            repaired = _repair_truncated_consolidated_payload(text[index:])
            if repaired is not None:
                candidate = repaired
            continue
        if _looks_like_consolidated_payload(payload):
            candidate = payload

    if candidate is not None:
        return candidate, None
    return None, first_error


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


def _consolidate_payload_with_error(
    current: dict,
    new_raw: list[dict],
    *,
    candidate_rows: list[dict] | None = None,
    require_evidence: bool = False,
    attribution: dict | None = None,
    meta_performance: dict | None = None,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
) -> tuple[dict | None, dict | None]:
    router = llm_router or build_consolidator_router_from_env(
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
    )
    candidates = build_curation_candidates(new_raw, candidate_rows=candidate_rows)
    prompt = build_consolidation_prompt(
        current,
        candidates,
        attribution=attribution,
        meta_performance=meta_performance,
    )
    attempts = max(1, int(max_attempts))
    last_error: dict | None = None
    for attempt in range(attempts):
        completion = router.complete(prompt, timeout_s=timeout_s)
        if isinstance(completion, llm.LlmFailure):
            last_error = _failure_from_llm(completion)
            if not completion.retryable:
                return None, last_error
        else:
            payload, parse_error = _parse_consolidated_json_from_text(completion.text)
            if parse_error is not None:
                return None, _failure_payload(
                    "invalid_json",
                    str(parse_error),
                    provider=completion.provider,
                    model=completion.model,
                    output=completion.text,
                )
            elif payload is None:
                return None, _failure_payload(
                    "invalid_payload",
                    "consolidateur returned non-object payload",
                    provider=completion.provider,
                    model=completion.model,
                    output=completion.text,
                )
            else:
                finalized, validation_error = _finalize_consolidated_payload(
                    payload,
                    current=current,
                    candidates=candidates,
                    require_evidence=require_evidence,
                )
                if finalized is not None:
                    normalized = normalize_consolidated(finalized, watermark=current.get("watermark"))
                    if normalized is not None:
                        return normalized, None
                detail = validation_error or _failure_payload(
                    "invalid_payload", "consolidateur returned invalid payload"
                )
                last_error = _failure_payload(
                    str(detail["error_code"]),
                    str(detail["error_message"]),
                    provider=completion.provider,
                    model=completion.model,
                    output=completion.text,
                )
        if attempt == attempts - 1:
            return None, last_error
    return None, last_error or _failure_payload("no_attempt", "no consolidation attempt was executed")


def consolidate_payload(
    current: dict,
    new_raw: list[dict],
    *,
    candidate_rows: list[dict] | None = None,
    require_evidence: bool | None = None,
    attribution: dict | None = None,
    meta_performance: dict | None = None,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
) -> dict | None:
    consolidated, _error = _consolidate_payload_with_error(
        current,
        new_raw,
        candidate_rows=candidate_rows,
        require_evidence=(candidate_rows is not None if require_evidence is None else require_evidence),
        attribution=attribution,
        meta_performance=meta_performance,
        llm_router=llm_router,
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
    )
    return consolidated


def maybe_consolidate(
    raw_store: RawLearningsStore,
    consolidated_store: ConsolidatedLearningsStore,
    *,
    threshold: int = DEFAULT_CONSOLIDATION_THRESHOLD,
    candidate_rows: list[dict] | None = None,
    curation_provider: object | None = None,
    attribution: dict | None = None,
    meta_performance: dict | None = None,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
    status_store: ConsolidationStatusStore | None = None,
    now: datetime | None = None,
) -> dict:
    """Consolidate a bounded, outcome-weighted candidate pool when due.

    ``candidate_rows`` is an optional enriched snapshot (normally from the
    derived FLAIR store).  ``curation_provider`` offers the same data lazily via
    ``select_candidates`` and may acknowledge durable inputs through
    ``mark_candidates_curated``.  Both seams are optional so the raw JSONL-only
    recovery path remains usable.
    """

    current = consolidated_store.read()
    current_for_curation = _with_global_rule_memrl(current, curation_provider)
    raw_rows = raw_store.all()
    new_raw = select_new_raw(raw_rows, watermark=current.get("watermark"))
    threshold = max(1, threshold)
    now = now or datetime.now(timezone.utc)
    curation_counts = _curation_counts(curation_provider)
    curation_due_reason = _curation_due_reason(
        curation_counts,
        threshold=threshold,
        now=now,
    )
    raw_due = len(new_raw) >= threshold
    if not raw_due and curation_due_reason is None:
        return {"triggered": False, "new_raw_count": len(new_raw)}

    status_store = status_store or _default_status_store(consolidated_store)
    status = status_store.read()
    last_failure = status.get("last_failure")
    requested_model = _resolved_consolidator_model(model)
    last_failure_requested_model = None
    if isinstance(last_failure, dict):
        last_failure_requested_model = last_failure.get("requested_model", last_failure.get("model"))
    if (
        isinstance(last_failure, dict)
        and last_failure.get("consolidated_watermark") == current.get("watermark")
        and last_failure_requested_model in (None, requested_model)
    ):
        raw_since_failure = select_new_raw(raw_rows, watermark=last_failure.get("raw_watermark"))
        retry_after_new_raw = threshold
        if len(raw_since_failure) < retry_after_new_raw:
            return {
                "triggered": False,
                "new_raw_count": len(new_raw),
                "skipped": True,
                "reason": "previous_failure_backoff",
                "new_raw_since_failure": len(raw_since_failure),
                "retry_after_new_raw": retry_after_new_raw,
                "last_error_code": last_failure.get("error_code"),
            }

    resolved_candidate_rows = candidate_rows
    if resolved_candidate_rows is None and curation_provider is not None:
        try:
            resolved_candidate_rows = _call_curation_provider(
                curation_provider,
                current=current_for_curation,
                new_raw=new_raw,
            )
        except Exception as exc:
            error = _failure_payload("curation_provider", str(exc))
            error = {**error, "requested_model": requested_model}
            status_store.write_failure(
                consolidated_watermark=current.get("watermark"),
                raw_watermark=_max_ts(new_raw),
                new_raw_count=len(new_raw),
                error=error,
            )
            return _failure_result(error, new_raw_count=len(new_raw))

    consolidated, error = _consolidate_payload_with_error(
        current_for_curation,
        new_raw,
        candidate_rows=resolved_candidate_rows,
        require_evidence=resolved_candidate_rows is not None,
        attribution=attribution,
        meta_performance=meta_performance,
        llm_router=llm_router,
        acpx_bin=acpx_bin,
        acpx_agent=acpx_agent,
        model=model,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
    )
    if consolidated is None:
        error = error or _failure_payload("unknown", "unknown consolidation failure")
        error = {**error, "requested_model": requested_model}
        status_store.write_failure(
            consolidated_watermark=current.get("watermark"),
            raw_watermark=_max_ts(new_raw),
            new_raw_count=len(new_raw),
            error=error,
        )
        return _failure_result(error, new_raw_count=len(new_raw))

    watermark = _max_ts(new_raw) or current.get("watermark")
    consolidated_store.write(consolidated, watermark=watermark)
    status_store.clear()
    result = {
        "triggered": True,
        "new_raw_count": len(new_raw),
        "written": True,
        **({"curation_due": curation_due_reason} if curation_due_reason else {}),
    }
    if curation_provider is not None:
        global_rule_sync = _sync_active_global_rules(
            curation_provider,
            consolidated=consolidated,
        )
        candidates = build_curation_candidates(new_raw, candidate_rows=resolved_candidate_rows)
        result["curated_candidate_count"] = _mark_candidates_curated(
            curation_provider,
            candidates=candidates,
            source_rows=resolved_candidate_rows,
            watermark=watermark,
        )
        if global_rule_sync is not None:
            result["global_rule_sync"] = global_rule_sync
    return result


def _default_state_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "state"


def main(argv: list[str] | None = None) -> int:
    """CLI d'inspection et run cron : python -m trader.agent.learnings.consolidator --run."""
    import argparse

    parser = argparse.ArgumentParser(description="Consolidateur des learnings runtime")
    parser.add_argument("--run", action="store_true", help="exécute une consolidation si le seuil est atteint")
    parser.add_argument("--state-dir", type=Path, default=_default_state_dir(), help="répertoire state/ à utiliser")
    parser.add_argument("--threshold", type=int, default=DEFAULT_CONSOLIDATION_THRESHOLD, help="seuil de bruts nouveaux")
    parser.add_argument("--max-attempts", type=int, default=3, help="tentatives LLM intra-cycle")
    parser.add_argument("--acpx-bin", default=None, help="binaire acpx du consolidateur")
    parser.add_argument("--acpx-agent", default=None, help="agent acpx du consolidateur")
    parser.add_argument("--model", default=None, help="modèle du consolidateur")
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_CONSOLIDATOR_TIMEOUT_S, help="timeout LLM en secondes")
    parser.add_argument("--attribution-since", default=None, help="borne since deja filtree au regime")
    parser.add_argument(
        "--exclude-symbol",
        action="append",
        default=[],
        help="symbole a exclure de l'attribution ; repetable",
    )
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    raw_store = RawLearningsStore(state_dir / "learnings.jsonl", max_entries=DEFAULT_RAW_MAX_ENTRIES)
    consolidated_store = ConsolidatedLearningsStore(state_dir / "learnings_consolidated.json")

    if args.run:
        consolidation_inputs = import_module("trader.runtime.consolidation_inputs")
        attr, meta = consolidation_inputs.build_consolidation_inputs(
            state_dir=state_dir,
            risk_yaml_path=Path(__file__).resolve().parents[3] / "config" / "risk.yaml",
            attribution_since=args.attribution_since,
            exclude_symbols=tuple(args.exclude_symbol),
        )
        result = maybe_consolidate(
            raw_store,
            consolidated_store,
            threshold=args.threshold,
            attribution=attr,
            meta_performance=meta,
            acpx_bin=args.acpx_bin,
            acpx_agent=args.acpx_agent,
            model=args.model,
            timeout_s=args.timeout_s,
            max_attempts=args.max_attempts,
        )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0

    current = consolidated_store.read()
    raw_rows = raw_store.all()
    payload = {
        "state_dir": str(state_dir),
        "raw_count": len(raw_rows),
        "new_raw_count": pending_raw_count(raw_rows, watermark=current.get("watermark")),
        "consolidated_watermark": current.get("watermark"),
        "has_consolidated": _has_consolidated(current),
        "last_failure": _default_status_store(consolidated_store).read().get("last_failure"),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
