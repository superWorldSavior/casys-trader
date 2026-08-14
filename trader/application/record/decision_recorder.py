"""Decision recording service for the runtime cycle."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeAlias

from trader.application.record.decision_entries import has_exploitable_llm_rationale
from trader.application.record.decision_ledger_rows import build_decision_row
from trader.application.record.tool_outcomes import finalize_action_tool_outcomes
from trader.domain import decision_identity

log = logging.getLogger("trader.application.decision_recorder")

DecisionEntry: TypeAlias = dict[str, Any]
ReportPayload: TypeAlias = dict[str, Any]
StatusWriter: TypeAlias = Callable[..., None]
EventAppender: TypeAlias = Callable[..., None]
NewsSnapshotProvider: TypeAlias = Callable[[str, datetime], ReportPayload]
BriefRefProvider: TypeAlias = Callable[[str, datetime], dict[str, str] | None]
ResearchSliceProvider: TypeAlias = Callable[[str], dict[str, Any] | None]
MergeGateFeedback: TypeAlias = Callable[[object, object, object], str | None]
LearningIngester: TypeAlias = Callable[[], None]
AgentTraceAppender: TypeAlias = Callable[[DecisionEntry, str], None]


def _entry_price(decision_entry: DecisionEntry, report: ReportPayload, symbol: str) -> float | None:
    raw = decision_entry.get("price")
    if raw is None:
        prices = report.get("prices")
        if isinstance(prices, dict):
            raw = prices.get(symbol)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str | None:
    """Return a non-empty normalized text field without coercing malformed input."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


_TRADE_ACTIONS = frozenset(
    {
        "BUY",
        "SELL",
        "OPEN_LONG",
        "OPEN_SHORT",
        "FLIP",
        "SCALE_IN",
        "CLOSE",
        "REDUCE",
    }
)
_REFUSED_THESIS_SIDES = frozenset({"long", "short"})


def _is_llm_rationale_candidate(decision_entry: DecisionEntry) -> bool:
    """Gate automatic capture to actual, usable LLM decisions only."""
    return (
        decision_entry.get("decision_source") == "llm"
        and decision_entry.get("model_called") is True
        and has_exploitable_llm_rationale(
            rationale=decision_entry.get("rationale"),
            llm_error=decision_entry.get("llm_error"),
        )
    )


def _refused_opportunity_side(decision_entry: DecisionEntry) -> bool:
    raw = decision_entry.get("opportunity_side")
    if not isinstance(raw, str):
        return False
    return raw.strip().lower() in _REFUSED_THESIS_SIDES


def _is_undirected_hold(decision_entry: DecisionEntry) -> bool:
    """True for a HOLD that is not also a trade action/intent."""
    action = decision_entry.get("action")
    intent = decision_entry.get("intent")
    if action in _TRADE_ACTIONS or intent in _TRADE_ACTIONS:
        return False
    return action == "HOLD" or intent == "HOLD"


def candidate_learning_note(
    decision_entry: DecisionEntry,
) -> tuple[str | None, str | None, str | None]:
    """Build the one deduplicable candidate note for a recorded decision.

    A real LLM rationale is the canonical experience.  An optional explicit
    ``record_learning`` annotation is retained in the same note because the
    derived store keys notes by ``decision_id``.

    D6: ingesting every authentic LLM HOLD feeds an abstention spiral. A HOLD
    is captured only when it names a refused thesis (``opportunity_side``
    long/short) or carries an explicit learning annotation.
    """
    annotation = _text(decision_entry.get("learning"))
    rationale = _text(decision_entry.get("rationale"))
    if not _is_llm_rationale_candidate(decision_entry):
        return None, None, None
    if _is_undirected_hold(decision_entry) and not annotation and not _refused_opportunity_side(
        decision_entry
    ):
        return None, None, None
    if annotation and annotation != rationale:
        return f"{rationale}\n[annotation explicite] {annotation}", rationale, annotation
    return rationale, rationale, annotation


class LearningAppender(Protocol):
    def append(self, *, symbol: str, note: str, now: datetime, **extra: object) -> object:
        ...


class DecisionLedgerAppender(Protocol):
    """Write port required by the decision-recording use case."""

    def append(self, row: dict) -> object:
        ...


class RecallRecorder(Protocol):
    def record_recall(self, *, decision_id: str, note_ids: list[int]) -> None:
        ...

    def record_global_rule_citation(
        self,
        *,
        decision_id: str,
        rule_ids: list[str],
        ts: str | None = None,
    ) -> bool:
        ...


@dataclass
class DecisionRecorder:
    report: ReportPayload
    dry_run: bool
    symbols_total: int
    max_model_calls_per_cycle: int | None
    learnings_store: LearningAppender
    decision_ledger_store: DecisionLedgerAppender
    refresh_report_portfolio: Callable[[], None]
    write_current_report: Callable[[ReportPayload], None]
    write_status: StatusWriter
    append_event: EventAppender
    news_snapshot: NewsSnapshotProvider
    macro_next: ReportPayload | None
    now: datetime
    brief_ref_provider: BriefRefProvider | None = None
    recall_store: RecallRecorder | None = None
    merge_gate_feedback: MergeGateFeedback | None = None
    model_calls_used_getter: Callable[[], int] = lambda: 0
    agent_trace_appender: AgentTraceAppender | None = None
    company_context_provider: ResearchSliceProvider | None = None
    mandate_context_provider: ResearchSliceProvider | None = None
    learning_ingester: LearningIngester | None = None

    def record(self, decision_entry: DecisionEntry) -> None:
        symbol = str(decision_entry["symbol"])
        sequence = len(self.report["decisions"])
        cycle_ts = str(self.report.get("ts") or "")
        decision_id = str(
            decision_entry.get("decision_id")
            or decision_identity.decision_id(cycle_ts, sequence, symbol)
        )
        decision_entry["decision_id"] = decision_id
        price = _entry_price(decision_entry, self.report, symbol)
        if price is not None:
            decision_entry["price"] = price
        # Réécrit l'outcome des action tools finaux avec le résultat réel de la
        # décision (le brut naît "ok"). Ne touche pas les outils de tournée (recall).
        decision_entry["tool_calls"] = finalize_action_tool_outcomes(decision_entry)
        if self.agent_trace_appender is not None:
            try:
                self.agent_trace_appender(decision_entry, str(self.report.get("ts") or ""))
            except Exception as exc:  # noqa: BLE001 - trace must not impede a decision record
                log.warning("agent trace append failed: %s", exc)
        decision_entry.setdefault("news", self.news_snapshot(symbol, self.now))
        news = decision_entry.get("news")
        if isinstance(news, dict) and "macro_next" not in news:
            decision_entry["news"] = {**news, "macro_next": self.macro_next}
            news = decision_entry["news"]
        if isinstance(news, dict) and "brief_ref" not in news and self.brief_ref_provider is not None:
            brief_ref = self.brief_ref_provider(symbol, self.now)
            if brief_ref is not None:
                decision_entry["news"] = {**news, "brief_ref": brief_ref}

        if self.company_context_provider is not None:
            company_context = self.company_context_provider(symbol)
            if isinstance(company_context, dict):
                company_ref = company_context.get("brief_ref")
                decision_entry["company_context_status"] = company_context.get("status")
                decision_entry["company_brief_refs"] = (
                    [dict(company_ref)] if isinstance(company_ref, dict) else []
                )
        if self.mandate_context_provider is not None:
            mandate_context = self.mandate_context_provider(symbol)
            if isinstance(mandate_context, dict):
                mandate_ref = mandate_context.get("mandate_ref")
                decision_entry["mandate_ref"] = (
                    dict(mandate_ref) if isinstance(mandate_ref, dict) else None
                )

        note, rationale, annotation = candidate_learning_note(decision_entry)
        if note and self.merge_gate_feedback is not None:
            merged_note = self.merge_gate_feedback(
                decision_entry.get("reason"),
                decision_entry.get("context"),
                note,
            )
            note = _text(merged_note) or note
        if note:
            learning_extra: dict[str, object] = {}
            if rationale is not None:
                learning_extra["rationale"] = rationale
            if annotation is not None:
                learning_extra["learning_annotation"] = annotation
            decision_entry["learning_recorded"] = self.learnings_store.append(
                symbol=symbol,
                note=note,
                now=self.now,
                decision_id=decision_id,
                action=decision_entry.get("action"),
                intent=decision_entry.get("intent"),
                executed=decision_entry.get("executed"),
                reason=decision_entry.get("reason"),
                dry_run=self.dry_run,
                **learning_extra,
            )
            # Store the raw note in the derived RAG immediately as pending.
            # Vector enrichment / FLAIR remain background best-effort work,
            # but a subsequent explicit recall can already find it via FTS.
            if decision_entry["learning_recorded"] and self.learning_ingester is not None:
                try:
                    self.learning_ingester()
                except Exception as exc:  # noqa: BLE001 - never delay a decision record
                    log.warning("learning immediate ingest failed: %s", exc)

        self.report["decisions"].append(decision_entry)
        self.report["model_calls_used"] = self.model_calls_used_getter()
        self.refresh_report_portfolio()

        row = build_decision_row(
            self.report,
            decision_entry,
            sequence=sequence,
            source="armed_plan" if decision_entry.get("armed_plan_id") else "daemon",
        )
        appended = self.decision_ledger_store.append(row)
        log.debug(
            "decision_recorder ledger_write symbol=%s sequence=%d appended=%s",
            symbol,
            sequence,
            appended,
        )

        self._record_recall_trace(decision_entry, sequence)
        self.write_current_report(self.report)
        self.write_status(
            "decision_recorded",
            current_symbol=symbol,
            decisions_done=len(self.report["decisions"]),
            symbols_total=self.symbols_total,
            last_decision=decision_entry,
            model_calls_used=self.report["model_calls_used"],
            max_model_calls_per_cycle=self.max_model_calls_per_cycle,
        )
        self.append_event(
            "decision_recorded",
            symbol=symbol,
            action=decision_entry.get("action"),
            reason=decision_entry.get("reason"),
            executed=decision_entry.get("executed"),
        )
        log.debug(
            "decision_recorder recorded symbol=%s reason=%s executed=%s",
            symbol,
            decision_entry.get("reason"),
            decision_entry.get("executed"),
        )

    def _record_recall_trace(self, decision_entry: DecisionEntry, sequence: int) -> None:
        if self.recall_store is None:
            return
        cycle_ts = str(self.report.get("ts") or "")
        symbol = str(decision_entry.get("symbol") or "")
        if not cycle_ts or not symbol:
            return
        decision_id = str(
            decision_entry.get("decision_id")
            or decision_identity.decision_id(cycle_ts, sequence, symbol)
        )
        for tool_call in decision_entry.get("tool_calls") or []:
            if tool_call.get("tool") != "recall_learnings" or tool_call.get("outcome") != "ok":
                continue
            note_ids = [
                note_id
                for note_id in (tool_call.get("detail") or {}).get("note_ids", [])
                if isinstance(note_id, int)
            ]
            if not note_ids:
                continue
            try:
                self.recall_store.record_recall(decision_id=decision_id, note_ids=note_ids)
            except Exception as exc:  # noqa: BLE001
                log.warning("record_recall failed: %s", exc)

        rule_ids = decision_entry.get("applied_learning_ids")
        record_citation = getattr(self.recall_store, "record_global_rule_citation", None)
        if not isinstance(rule_ids, list) or not callable(record_citation):
            return
        citations = [rule_id for rule_id in rule_ids if isinstance(rule_id, str)]
        if not citations:
            return
        try:
            record_citation(
                decision_id=decision_id,
                rule_ids=citations,
                ts=cycle_ts,
            )
        except Exception as exc:  # noqa: BLE001 - citations never impede a trade
            log.warning("record_global_rule_citation failed: %s", exc)
