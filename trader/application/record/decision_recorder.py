"""Decision recording service for the runtime cycle."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from trader.application.record.tool_outcomes import finalize_action_tool_outcomes
from trader.reporting.ledger import decision_ledger
from trader.reporting.ledger.protocols import DecisionLedgerAppender

log = logging.getLogger("trader.application.decision_recorder")

DecisionEntry: TypeAlias = dict[str, Any]
ReportPayload: TypeAlias = dict[str, Any]
StatusWriter: TypeAlias = Callable[..., None]
EventAppender: TypeAlias = Callable[..., None]
NewsSnapshotProvider: TypeAlias = Callable[[str, datetime], ReportPayload]
BriefRefProvider: TypeAlias = Callable[[str, datetime], dict[str, str] | None]
ResearchSliceProvider: TypeAlias = Callable[[str], dict[str, Any] | None]
MergeGateFeedback: TypeAlias = Callable[[object, object, object], str | None]


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


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _model_label(decision_entry: DecisionEntry) -> str:
    provider = decision_entry.get("llm_provider")
    model = decision_entry.get("llm_model")
    if provider and model:
        return f"{provider}:{model}"
    if model:
        return str(model)
    if provider:
        return str(provider)
    return "-"


def _agent_trace_source(decision_entry: DecisionEntry) -> str:
    source = decision_entry.get("decision_source")
    if source:
        return str(source)
    if decision_entry.get("armed_plan_id"):
        return "armed_plan"
    return "daemon"


def _agent_trace_lines(decision_entry: DecisionEntry, cycle_ts: str) -> list[str]:
    calls = decision_entry.get("tool_calls")
    tool_calls = calls if isinstance(calls, list) else []
    source = _agent_trace_source(decision_entry)
    if not (decision_entry.get("model_called") or tool_calls or source == "armed_plan"):
        return []

    symbol = str(decision_entry.get("symbol") or "")
    lines = [
        (
            f"[agent] ts={cycle_ts} symbol={symbol} source={source} model={_model_label(decision_entry)} "
            f"action={decision_entry.get('action')} intent={decision_entry.get('intent')} "
            f"reason={decision_entry.get('reason')} executed={decision_entry.get('executed')} "
            f"tools={len(tool_calls)} rounds={decision_entry.get('tool_rounds', 0)}"
        )
    ]
    automatic_recall = decision_entry.get("automatic_recall")
    if isinstance(automatic_recall, dict):
        lines.append(
            f"[agent-memory] ts={cycle_ts} symbol={symbol} kind=flair_experience "
            f"detail={_compact_json(automatic_recall)}"
        )
    lines.extend(
        (
            f"[agent-tool] ts={cycle_ts} symbol={symbol} id={call.get('id')} "
            f"tool={call.get('tool')} outcome={call.get('outcome')} "
            f"args={_compact_json(call.get('args', {}))} detail={_compact_json(call.get('detail', {}))}"
        )
        for call in tool_calls
        if isinstance(call, dict)
    )
    return lines


def _append_agent_trace(path: Path, decision_entry: DecisionEntry, cycle_ts: str) -> None:
    lines = _agent_trace_lines(decision_entry, cycle_ts)
    if not lines:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(f"{line}\n")
    except OSError as exc:
        log.warning("agent_trace_write failed path=%s: %s", path, exc)


class LearningAppender(Protocol):
    def append(self, *, symbol: str, note: str, now: datetime, **extra: object) -> object:
        ...


class RecallRecorder(Protocol):
    def record_recall(self, *, decision_id: str, note_ids: list[int]) -> None:
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
    agent_trace_path: Path | None = None
    company_context_provider: ResearchSliceProvider | None = None
    mandate_context_provider: ResearchSliceProvider | None = None

    def record(self, decision_entry: DecisionEntry) -> None:
        symbol = str(decision_entry["symbol"])
        price = _entry_price(decision_entry, self.report, symbol)
        if price is not None:
            decision_entry["price"] = price
        # Réécrit l'outcome des action tools finaux avec le résultat réel de la
        # décision (le brut naît "ok"). Ne touche pas les outils de tournée (recall).
        decision_entry["tool_calls"] = finalize_action_tool_outcomes(decision_entry)
        if self.agent_trace_path is not None:
            _append_agent_trace(self.agent_trace_path, decision_entry, str(self.report.get("ts") or ""))
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

        if self.merge_gate_feedback is not None:
            note = self.merge_gate_feedback(
                decision_entry.get("reason"),
                decision_entry.get("context"),
                decision_entry.get("learning"),
            )
            if note:
                decision_entry["learning_recorded"] = self.learnings_store.append(
                    symbol=symbol,
                    note=note,
                    now=self.now,
                    action=decision_entry.get("action"),
                    intent=decision_entry.get("intent"),
                    executed=decision_entry.get("executed"),
                    reason=decision_entry.get("reason"),
                    dry_run=self.dry_run,
                )

        sequence = len(self.report["decisions"])
        self.report["decisions"].append(decision_entry)
        self.report["model_calls_used"] = self.model_calls_used_getter()
        self.refresh_report_portfolio()

        row = decision_ledger.build_decision_row(
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
        decision_id = decision_ledger._decision_id(cycle_ts, sequence, symbol)
        automatic_recall = decision_entry.get("automatic_recall")
        if isinstance(automatic_recall, dict):
            note_ids = [
                note_id
                for note_id in automatic_recall.get("note_ids", [])
                if isinstance(note_id, int)
            ]
            if note_ids:
                try:
                    self.recall_store.record_recall(
                        decision_id=decision_id,
                        note_ids=note_ids,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("record automatic recall failed: %s", exc)
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
