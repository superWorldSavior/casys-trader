"""Consolidateur machine-owned des learnings runtime.

Le store brut `learnings.jsonl` reste un buffer jetable. Ce module synthétise
périodiquement ces notes vers `learnings_consolidated.json`, séparé de la mémoire
humaine `mandate/memory.md`.
"""

from __future__ import annotations

import json
import logging
import os
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
)
from trader.agent.learnings.selection import _parse_ts, pending_raw_count, select_new_raw
from trader.agent.learnings.raw_store import RawLearningsStore

log = logging.getLogger(__name__)

DEFAULT_RAW_MAX_ENTRIES = 200
DEFAULT_CONSOLIDATION_THRESHOLD = 50
DEFAULT_CONSOLIDATOR_PROVIDER = "consolidator"
DEFAULT_CONSOLIDATOR_ACPX_AGENT = "codex"
DEFAULT_CONSOLIDATOR_MODEL = "gpt-5.5"
DEFAULT_CONSOLIDATOR_TIMEOUT_S = 240

__all__ = [
    "DEFAULT_CONSOLIDATION_THRESHOLD",
    "DEFAULT_CONSOLIDATOR_ACPX_AGENT",
    "DEFAULT_CONSOLIDATOR_MODEL",
    "DEFAULT_CONSOLIDATOR_PROVIDER",
    "DEFAULT_CONSOLIDATOR_TIMEOUT_S",
    "DEFAULT_MAX_BY_SYMBOL",
    "DEFAULT_MAX_GLOBAL",
    "DEFAULT_RAW_MAX_ENTRIES",
    "ConsolidatedLearningsStore",
    "ConsolidationStatusStore",
    "build_consolidation_prompt",
    "build_consolidator_router_from_env",
    "build_context_learnings",
    "consolidate_payload",
    "empty_consolidated",
    "load_guardrails",
    "maybe_consolidate",
    "normalize_consolidated",
]


def _max_ts(rows: list[dict]) -> str | None:
    parsed = [_parse_ts(row.get("ts")) for row in rows]
    valid = [item for item in parsed if item is not None]
    if not valid:
        return None
    return max(valid).isoformat()


def _has_consolidated(payload: dict) -> bool:
    return bool(payload.get("global") or payload.get("by_symbol"))


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
) -> list[dict] | dict:
    """Contexte learnings injecté à l'agent (D6 du registre).

    `guardrails` = invariants humains, toujours présents et nommés à part —
    l'agent doit pouvoir distinguer « règle qui ne bouge pas » de « pattern
    appris, remettable en question ». Dès qu'un consolidé existe, les bruts ne
    sont plus réinjectés : ils répétaient les derniers HOLD et nourrissaient la
    boucle d'auto-renforcement.
    """
    normalized = normalize_consolidated(consolidated, watermark=consolidated.get("watermark"))
    if normalized is None or not _has_consolidated(normalized):
        if guardrails:
            return {"guardrails": guardrails, "raw_recent": raw_recent}
        return raw_recent
    context: dict = {
        "global": normalized["global"],
        "by_symbol": normalized["by_symbol"],
    }
    if guardrails:
        context["guardrails"] = guardrails
    return context


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
    return isinstance(payload, dict) and "global" in payload and "by_symbol" in payload


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
    prompt = build_consolidation_prompt(
        current,
        new_raw,
        attribution=attribution,
        meta_performance=meta_performance,
    )
    attempts = max(1, int(max_attempts))
    completion: llm.LlmCompletion | llm.LlmFailure | None = None
    for attempt in range(attempts):
        completion = router.complete(prompt, timeout_s=timeout_s)
        if not isinstance(completion, llm.LlmFailure):
            break
        if not completion.retryable or attempt == attempts - 1:
            return None, _failure_from_llm(completion)
    if isinstance(completion, llm.LlmFailure):
        return None, _failure_from_llm(completion)
    if completion is None:
        return None, _failure_payload("no_attempt", "no consolidation attempt was executed")
    payload, parse_error = _parse_consolidated_json_from_text(completion.text)
    if parse_error is not None:
        return None, _failure_payload(
            "invalid_json",
            str(parse_error),
            provider=completion.provider,
            model=completion.model,
            output=completion.text,
        )
    if payload is None:
        return None, _failure_payload(
            "invalid_payload",
            "consolidateur returned non-object payload",
            provider=completion.provider,
            model=completion.model,
            output=completion.text,
        )
    normalized = normalize_consolidated(payload, watermark=current.get("watermark"))
    if normalized is None:
        return None, _failure_payload(
            "invalid_payload",
            "consolidateur returned invalid payload",
            provider=completion.provider,
            model=completion.model,
            output=completion.text,
        )
    return normalized, None


def consolidate_payload(
    current: dict,
    new_raw: list[dict],
    *,
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
    attribution: dict | None = None,
    meta_performance: dict | None = None,
    llm_router: llm.LlmRouter | None = None,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    max_attempts: int = 3,
    status_store: ConsolidationStatusStore | None = None,
) -> dict:
    current = consolidated_store.read()
    raw_rows = raw_store.all()
    new_raw = select_new_raw(raw_rows, watermark=current.get("watermark"))
    if len(new_raw) < max(1, threshold):
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
        retry_after_new_raw = max(1, threshold)
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

    consolidated, error = _consolidate_payload_with_error(
        current,
        new_raw,
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

    watermark = _max_ts(new_raw)
    consolidated_store.write(consolidated, watermark=watermark)
    status_store.clear()
    return {"triggered": True, "new_raw_count": len(new_raw), "written": True}


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
