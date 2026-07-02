"""Decision recording service for the runtime cycle."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeAlias

from trader import decision_ledger

log = logging.getLogger(__name__)

DecisionEntry: TypeAlias = dict[str, Any]
ReportPayload: TypeAlias = dict[str, Any]
StatusWriter: TypeAlias = Callable[..., None]
EventAppender: TypeAlias = Callable[..., None]
NewsSnapshotProvider: TypeAlias = Callable[[str, datetime], ReportPayload]
MergeGateFeedback: TypeAlias = Callable[[object, object, object], str | None]


class LearningAppender(Protocol):
    def append(self, *, symbol: str, note: str, now: datetime, **extra: object) -> object:
        ...


class DecisionLedgerAppender(Protocol):
    def append(self, row: ReportPayload) -> object:
        ...


class RecallRecorder(Protocol):
    def record_recall(self, *, decision_id: str, note_ids: list[int]) -> None:
        ...


@dataclass
class DecisionRecorder:
    report: ReportPayload
    dry_run: bool
    symbols_total: int
    max_model_calls_per_cycle: int
    learnings_store: LearningAppender
    decision_ledger_store: DecisionLedgerAppender
    refresh_report_portfolio: Callable[[], None]
    write_current_report: Callable[[ReportPayload], None]
    write_status: StatusWriter
    append_event: EventAppender
    news_snapshot: NewsSnapshotProvider
    macro_next: ReportPayload | None
    now: datetime
    recall_store: RecallRecorder | None = None
    merge_gate_feedback: MergeGateFeedback | None = None
    model_calls_used_getter: Callable[[], int] = lambda: 0

    def record(self, decision_entry: DecisionEntry) -> None:
        symbol = str(decision_entry["symbol"])
        decision_entry.setdefault("news", self.news_snapshot(symbol, self.now))
        news = decision_entry.get("news")
        if isinstance(news, dict) and "macro_next" not in news:
            decision_entry["news"] = {**news, "macro_next": self.macro_next}

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
