"""Runtime file adapter for the human-readable agent trace."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from trader.runtime.protocols import LoggerLike


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _model_label(decision_entry: dict[str, Any]) -> str:
    provider = decision_entry.get("llm_provider")
    model = decision_entry.get("llm_model")
    if provider and model:
        return f"{provider}:{model}"
    if model:
        return str(model)
    if provider:
        return str(provider)
    return "-"


def _agent_trace_source(decision_entry: dict[str, Any]) -> str:
    source = decision_entry.get("decision_source")
    if source:
        return str(source)
    if decision_entry.get("armed_plan_id"):
        return "armed_plan"
    return "daemon"


def agent_trace_lines(decision_entry: dict[str, Any], cycle_ts: str) -> list[str]:
    """Render the durable, human-readable trace lines for one decision."""
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
    applied_learning_ids = decision_entry.get("applied_learning_ids")
    if isinstance(applied_learning_ids, list) and applied_learning_ids:
        lines.append(
            f"[agent-learning] ts={cycle_ts} symbol={symbol} kind=global_rules "
            f"rule_ids={_compact_json(applied_learning_ids)}"
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


def build_agent_trace_appender(
    path: Path,
    *,
    logger: LoggerLike | None = None,
) -> Callable[[dict[str, Any], str], None]:
    """Bind the runtime trace file behind an application-facing callable."""
    log = logger or logging.getLogger("casys-trader")

    def append(decision_entry: dict[str, Any], cycle_ts: str) -> None:
        lines = agent_trace_lines(decision_entry, cycle_ts)
        if not lines:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(f"{line}\n")
        except OSError as exc:
            log.warning("agent_trace_write failed path=%s: %s", path, exc)

    return append


__all__ = ["agent_trace_lines", "build_agent_trace_appender"]
