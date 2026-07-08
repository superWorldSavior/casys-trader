"""Runtime finalizers for daemon end-of-cycle side effects."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Protocol, Sequence

from trader.runtime.protocols import LoggerLike


class LearningConsolidator(Protocol):
    def __call__(self, raw_store: object, consolidated_store: object, **kwargs: object) -> Mapping[str, object]: ...


class MacroCollector(Protocol):
    def __call__(self, state_dir: Path, now: datetime) -> Mapping[str, object]: ...


class StateComparator(Protocol):
    def __call__(self, state_dir: Path) -> Mapping[str, object]: ...


ReportWriter = Callable[[dict], None]
EventAppender = Callable[..., None]
GrossRejectionSummarizer = Callable[[Sequence[Mapping[str, object]]], dict | None]


@dataclass(frozen=True)
class LearningConsolidationRequest:
    raw_store: object
    consolidated_store: object
    threshold: int
    acpx_bin: str | None
    acpx_agent: str | None
    model: str | None
    timeout_s: int
    attribution: Mapping[str, object] | None
    meta_performance: Mapping[str, object] | None
    consolidate: LearningConsolidator


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


def default_state_comparator(state_dir: Path) -> Mapping[str, object]:
    from trader.infrastructure.state_db.compare import compare_backends

    return compare_backends(state_dir)


def run_learning_consolidation(
    request: LearningConsolidationRequest,
    *,
    report: dict,
    write_current_report: ReportWriter,
    append_event: EventAppender,
) -> dict:
    result = dict(
        request.consolidate(
            request.raw_store,
            request.consolidated_store,
            threshold=request.threshold,
            acpx_bin=request.acpx_bin,
            acpx_agent=request.acpx_agent,
            model=request.model,
            timeout_s=request.timeout_s,
            attribution=request.attribution,
            meta_performance=request.meta_performance,
        )
    )
    if result.get("triggered"):
        report["learning_consolidation"] = result
        write_current_report(report)
        append_event("learning_consolidated", **result)
    return result


def collect_macro_series(
    *,
    state_dir: Path,
    now: datetime,
    collect_macro: MacroCollector,
    logger: LoggerLike | None = None,
) -> dict | None:
    log = logger or _default_logger()
    try:
        result = dict(collect_macro(state_dir, now))
        if result.get("triggered"):
            log.debug(
                "macro_series: collected=%s skipped=%s errors=%s",
                result.get("collected", 0),
                result.get("skipped", 0),
                result.get("errors", 0),
            )
        return result
    except Exception:  # noqa: BLE001 - best-effort total, never impacts the cycle
        return None


def remember_gross_rejections(
    *,
    state_dir: Path,
    decisions: Sequence[Mapping[str, object]],
    gross_rejection_cache: MutableMapping[str, dict | None],
    summarize_gross_rejections: GrossRejectionSummarizer,
) -> dict | None:
    summary = summarize_gross_rejections(decisions)
    gross_rejection_cache[str(state_dir)] = summary
    return summary


def compare_state_backend(
    *,
    state_dir: Path,
    now: datetime,
    backend: str,
    compare_backends: StateComparator | None = None,
    logger: LoggerLike | None = None,
) -> dict | None:
    if backend.lower() != "sqlite":
        return None

    log = logger or _default_logger()
    compare = compare_backends or default_state_comparator
    try:
        result = dict(compare(state_dir))
        broker = result.get("broker", {})
        scheduler = result.get("scheduler", {})
        broker_map = broker if isinstance(broker, Mapping) else {}
        scheduler_map = scheduler if isinstance(scheduler, Mapping) else {}
        cash = broker_map.get("cash", {})
        cash_map = cash if isinstance(cash, Mapping) else {}
        log.info(
            "[state-compare] cycle=%s identical=%s cash=%s positions=%d "
            "plans=%d wakes=%d watches=%d stale=%d",
            now.isoformat(),
            result.get("identical"),
            cash_map.get("identical"),
            len(broker_map.get("positions_diff") or []),
            len((result.get("trade_plans", {}) or {}).get("diff") or []),
            len(scheduler_map.get("wakes_diff") or []),
            len(scheduler_map.get("watches_diff") or []),
            len(scheduler_map.get("stale_diff") or []),
        )
        if not result.get("identical"):
            log.warning("[state-compare] DIVERGENCE cycle=%s détail=%s", now.isoformat(), result)
        return result
    except Exception as exc:  # noqa: BLE001 - observation only
        log.warning("[state-compare] échec: %s", exc)
        return None


def finalize_cycle(
    *,
    state_dir: Path,
    now: datetime,
    report: dict,
    decidable_symbols: Sequence[str],
    decided_symbols: Sequence[str],
    learning: LearningConsolidationRequest | None,
    gross_rejection_cache: MutableMapping[str, dict | None],
    summarize_gross_rejections: GrossRejectionSummarizer,
    collect_macro: MacroCollector,
    state_backend: str,
    write_current_report: ReportWriter,
    append_event: EventAppender,
    logger: LoggerLike | None = None,
    compare_backends: StateComparator | None = None,
) -> None:
    log = logger or _default_logger()
    if learning is not None:
        run_learning_consolidation(
            learning,
            report=report,
            write_current_report=write_current_report,
            append_event=append_event,
        )

    collect_macro_series(state_dir=state_dir, now=now, collect_macro=collect_macro, logger=log)
    remember_gross_rejections(
        state_dir=state_dir,
        decisions=report["decisions"],
        gross_rejection_cache=gross_rejection_cache,
        summarize_gross_rejections=summarize_gross_rejections,
    )
    compare_state_backend(
        state_dir=state_dir,
        now=now,
        backend=state_backend,
        compare_backends=compare_backends,
        logger=log,
    )
