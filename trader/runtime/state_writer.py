"""File-backed runtime state writers for the daemon."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class RuntimeStateWriter:
    state_dir: Path
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    pid_fn: Callable[[], int] = os.getpid

    def write_json_state(self, filename: str, payload: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / filename).write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    def write_status(self, phase: str, **payload: object) -> None:
        self.write_json_state(
            "daemon_status.json",
            {
                "ts": self.now_fn().isoformat(),
                "phase": phase,
                "pid": self.pid_fn(),
                **payload,
            },
        )

    def write_current_report(self, report: dict) -> None:
        self.write_json_state("current_report.json", report)

    def write_last_report(self, report: dict) -> None:
        self.write_json_state("last_report.json", report)

    def append_event(self, event: str, **payload: object) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        row = {"ts": self.now_fn().isoformat(), "event": event, **payload}
        with (self.state_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def append_model_performance(self, **payload: object) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "model_performance.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def append_cycle_history(self, report: dict) -> None:
        portfolio = report.get("portfolio")
        n_executed_decisions = sum(1 for decision in report.get("decisions", []) if decision.get("executed"))
        n_executed_planned = sum(1 for item in report.get("planned_exits", []) if item.get("executed"))
        history_row = {
            "ts": report["ts"],
            "equity": portfolio["equity"] if portfolio is not None else None,
            "cash": portfolio["cash"] if portfolio is not None else None,
            "n_decisions": len(report.get("decisions", [])),
            "n_executed": n_executed_decisions + n_executed_planned,
            "n_planned_exits": len(report.get("planned_exits", [])),
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(history_row, ensure_ascii=False) + "\n")
