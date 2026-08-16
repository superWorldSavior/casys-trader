"""Composition adapter for the learnings consolidator.

Domain owns policy. Application owns the use case and outbound ports.
This module is the agent-facing façade: JSON stores, env LLM factory,
guardrails file, and the historical ``maybe_consolidate(raw, store, **kwargs)``
signature the daemon/tests already call.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from importlib import import_module
from pathlib import Path

from trader.agent import llm
from trader.agent.learnings.composer import LlmLearningComposer
from trader.agent.learnings.consolidation_prompt import build_consolidation_prompt
from trader.agent.learnings.consolidation_stores import (
    ConsolidatedLearningsStore,
    ConsolidationStatusStore,
)
from trader.agent.learnings.curation_adapter import as_curation_port
from trader.agent.learnings.raw_store import RawLearningsStore
from trader.application.learnings.consolidate import (
    DEFAULT_CONSOLIDATION_THRESHOLD,
    DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    consolidate_payload as _consolidate_payload,
    maybe_consolidate as _maybe_consolidate,
)
from trader.application.learnings.context import build_context_learnings, project_global_rules
from trader.domain.learnings.consolidation import (
    DEFAULT_CURATION_CATCH_UP_AGE,
    DEFAULT_FEEDBACK_CONSOLIDATION_THRESHOLD,
    DEFAULT_MAX_BY_SYMBOL,
    DEFAULT_MAX_GLOBAL,
    DEFAULT_RULE_GRACE_AGE,
    DEFAULT_RULE_MEASURED_UPDATES,
    empty_consolidated,
    normalize_consolidated,
    stable_rule_id,
)
from trader.domain.learnings.curation import (
    DEFAULT_MAX_CONFIRMATION_CANDIDATES,
    DEFAULT_MAX_COUNTEREXAMPLE_CANDIDATES,
    DEFAULT_MAX_CURATION_CANDIDATES,
    DEFAULT_MAX_RECENT_CANDIDATES,
    DEFAULT_SOFT_MAX_CANDIDATES_PER_SYMBOL,
    build_curation_candidates,
)
from trader.domain.learnings.scoring import (
    CITATION_UTILITY_HELPS,
    CITATION_UTILITY_HURTS,
    CITATION_UTILITY_NEUTRAL,
    CITATION_UTILITY_UNKNOWN,
    citation_utility,
)
from trader.domain.learnings.selection import pending_raw_count, select_new_raw

DEFAULT_RAW_MAX_ENTRIES = 200
DEFAULT_CONSOLIDATOR_PROVIDER = "consolidator"
DEFAULT_CONSOLIDATOR_ACPX_AGENT = "codex"
DEFAULT_CONSOLIDATOR_MODEL = "gpt-5.6-sol"

__all__ = [
    "DEFAULT_CONSOLIDATION_THRESHOLD",
    "DEFAULT_CONSOLIDATOR_ACPX_AGENT",
    "DEFAULT_CONSOLIDATOR_MODEL",
    "DEFAULT_CONSOLIDATOR_PROVIDER",
    "DEFAULT_CONSOLIDATOR_TIMEOUT_S",
    "DEFAULT_CURATION_CATCH_UP_AGE",
    "DEFAULT_FEEDBACK_CONSOLIDATION_THRESHOLD",
    "DEFAULT_MAX_CONFIRMATION_CANDIDATES",
    "DEFAULT_MAX_COUNTEREXAMPLE_CANDIDATES",
    "DEFAULT_MAX_CURATION_CANDIDATES",
    "DEFAULT_MAX_RECENT_CANDIDATES",
    "DEFAULT_MAX_BY_SYMBOL",
    "DEFAULT_MAX_GLOBAL",
    "DEFAULT_RAW_MAX_ENTRIES",
    "DEFAULT_RULE_GRACE_AGE",
    "DEFAULT_RULE_MEASURED_UPDATES",
    "DEFAULT_SOFT_MAX_CANDIDATES_PER_SYMBOL",
    "CITATION_UTILITY_HELPS",
    "CITATION_UTILITY_HURTS",
    "CITATION_UTILITY_NEUTRAL",
    "CITATION_UTILITY_UNKNOWN",
    "ConsolidatedLearningsStore",
    "ConsolidationStatusStore",
    "build_consolidation_prompt",
    "build_curation_candidates",
    "build_consolidator_router_from_env",
    "build_context_learnings",
    "citation_utility",
    "consolidate_payload",
    "empty_consolidated",
    "load_guardrails",
    "maybe_consolidate",
    "normalize_consolidated",
    "project_global_rules",
    "select_new_raw",
    "stable_rule_id",
]


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
    now: datetime | None = None,
) -> dict | None:
    composer = LlmLearningComposer(
        llm_router
        or build_consolidator_router_from_env(
            acpx_bin=acpx_bin,
            acpx_agent=acpx_agent,
            model=model,
        )
    )
    return _consolidate_payload(
        current,
        new_raw,
        composer=composer,
        candidate_rows=candidate_rows,
        require_evidence=require_evidence,
        attribution=attribution,
        meta_performance=meta_performance,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        now=now,
    )


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
    """Wire stores and the LLM composer, then run the application use case."""

    composer = LlmLearningComposer(
        llm_router
        or build_consolidator_router_from_env(
            acpx_bin=acpx_bin,
            acpx_agent=acpx_agent,
            model=model,
        )
    )
    return _maybe_consolidate(
        raw=raw_store,
        consolidated=consolidated_store,
        status=status_store or _default_status_store(consolidated_store),
        composer=composer,
        curation=as_curation_port(curation_provider),
        threshold=threshold,
        candidate_rows=candidate_rows,
        attribution=attribution,
        meta_performance=meta_performance,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        now=now,
        requested_model=_resolved_consolidator_model(model),
    )


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
