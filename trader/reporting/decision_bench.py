"""Decision bench reporting facade.

Moteur canonique : `trader.reporting.bench.decision_bench`.
Ce module garde la compatibilité d'import historique.
"""

from __future__ import annotations

from trader.reporting.bench.decision_bench import (
    DEFAULT_BENCH_MODELS,
    DEFAULT_VERDICTS,
    VALID_ACTIONS,
    ModelBenchFailure,
    ModelCompletion,
    ModelSpec,
    build_prompt,
    complete_model,
    dry_run_payload,
    load_reconstruction_history,
    parse_model_specs,
    parse_verdicts,
    reconstruct_case_contexts,
    render_summary,
    run_bench,
    score_model_reviews,
    select_cases,
)

__all__ = [
    "DEFAULT_BENCH_MODELS",
    "DEFAULT_VERDICTS",
    "VALID_ACTIONS",
    "ModelBenchFailure",
    "ModelCompletion",
    "ModelSpec",
    "build_prompt",
    "complete_model",
    "dry_run_payload",
    "load_reconstruction_history",
    "parse_model_specs",
    "parse_verdicts",
    "reconstruct_case_contexts",
    "render_summary",
    "run_bench",
    "score_model_reviews",
    "select_cases",
]
