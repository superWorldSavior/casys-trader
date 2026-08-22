#!/usr/bin/env python3
"""Reconstruct an observational Brain/Universe trace dataset.

This is an offline, read-only audit/export tool.  It never opens a runtime
database for writing and it does not change the daemon, prompts, scheduler or
execution path.  Its output is not a world-model transition dataset: a later
scheduled task is diagnostic chronology, never an atomic or fixed-horizon
market state.

The useful hierarchy is kept explicit::

    queue task
      -> queue attempt / raw Grok session
        -> ACP turn
          -> native Grok CLI steps
        -> Trader domain-tool rounds
      -> persisted decision
      -> process effects / fills
      -> later learning outcome

By default the command only prints a coverage report.  ``--output-dir`` is an
explicit opt-in that writes a reconstructed research dataset to an empty
directory outside the runtime state.

Example (recent Grok cohort)::

    uv run python scripts/brain_trajectory_spike.py \
      --task-id-min 11214 --task-id-max 11312 --json

    out=$(mktemp -d) && uv run python scripts/brain_trajectory_spike.py \
      --task-id-min 11214 --task-id-max 11312 \
      --include-shared-context --output-dir "$out" --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


SCHEMA_VERSION = "brain_trajectory_spike_v2"
CROSS_LOOP_SCHEMA_VERSION = "cross_loop_episode_v1"
_LINEAGE_STATUSES = frozenset({"exact", "reconstructed", "absent"})
_MANDATE_REF_FIELDS = (
    "mandate_id",
    "candidate_scope_id",
    "venue",
    "agent_run_id",
    "as_of",
    "status",
)
_OPENING_INTENTS = frozenset({"OPEN_LONG", "OPEN_SHORT", "SCALE_IN", "FLIP"})
_VERDICT_REWARD = {"WIN": 1.0, "LOSS": -1.0, "NEUTRAL": 0.0}
_PROMPT_CONTEXT_MARKER = "# Contexte partagé (JSON)"
_PROMPT_SYMBOLS_MARKER = "# Symboles à décider (JSON)"
_DOMAIN_RESULTS_MARKER = "# Nouveaux résultats (JSON)"
_ACPX_CALL_RE = re.compile(
    r"\[acpx_call\].*?session=(?P<task_id>\d+):(?P<slot>\d+)\s+"
    r"symbol=(?P<symbol>\S+)\s+.*?provider=(?P<provider>\S+)\s+"
    r"timeout_s=(?P<timeout>[0-9.]+)\s+dur_s=(?P<duration>[0-9.]+)\s+"
    r"outcome=(?P<outcome>\S+)"
)
_QUEUE_RETRY_RE = re.compile(
    r"\[queue\.ledger\]\s+retry\s+kind=decide\s+symbol=(?P<symbol>\S+)\s+"
    r"id=(?P<task_id>\d+)\s+attempt=(?P<attempt>\d+)/(?P<max_attempts>\d+)\s+"
    r".*?error=(?P<error>.+)$"
)


@dataclass(frozen=True)
class SpikeConfig:
    state_dir: Path
    sessions_dir: Path
    daemon_log: Path
    task_id_min: int | None = None
    task_id_max: int | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_shared_context: bool = False
    output_dir: Path | None = None


def _parse_iso(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _epoch_ms(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _json_load(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return None


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _contains_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_nonfinite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_nonfinite(item) for item in value)
    return False


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _sqlite_ro(path: Path) -> sqlite3.Connection:
    wal_path = Path(str(path) + "-wal")
    if wal_path.is_file() and wal_path.stat().st_size > 0:
        raise RuntimeError(f"offline spike refuses database with a non-empty WAL: {wal_path}")
    encoded = quote(str(path.resolve()), safe="/")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def _extract_json(text: str, *, marker: str | None = None) -> Any:
    if marker is not None:
        marker_at = text.find(marker)
        if marker_at < 0:
            return None
        text = text[marker_at + len(marker) :]
    decoder = json.JSONDecoder()
    candidates = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
    for idx in sorted(candidates):
        try:
            value, _end = decoder.raw_decode(text[idx:].lstrip())
            return value
        except json.JSONDecodeError:
            continue
    return None


def _assistant_payload(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    value = _extract_json(stripped)
    return value if isinstance(value, dict) else None


def _prompt_identity(prompt_path: Path) -> dict[str, Any]:
    if not prompt_path.is_file():
        return {
            "status": "missing",
            "cycle_id": None,
            "symbol": None,
            "shared_context": None,
            "symbol_block": None,
            "prompt_chars": 0,
            "prompt_sha256": None,
        }
    try:
        text = prompt_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "status": "unreadable",
            "error": str(exc),
            "cycle_id": None,
            "symbol": None,
            "shared_context": None,
            "symbol_block": None,
            "prompt_chars": 0,
            "prompt_sha256": None,
        }
    context = _extract_json(text, marker=_PROMPT_CONTEXT_MARKER)
    scope = context.get("decision_context_scope") if isinstance(context, dict) else None
    symbol = scope.get("symbol") if isinstance(scope, dict) else None
    cycle_id = context.get("now") if isinstance(context, dict) else None
    symbol_rows = _extract_json(text, marker=_PROMPT_SYMBOLS_MARKER)
    symbol_block = None
    if isinstance(symbol_rows, list) and symbol:
        symbol_block = next(
            (row for row in symbol_rows if isinstance(row, dict) and str(row.get("symbol") or "") == str(symbol)),
            None,
        )
    return {
        "status": (
            "ok"
            if isinstance(context, dict) and symbol and cycle_id and isinstance(symbol_block, dict)
            else "parse_error"
        ),
        "cycle_id": str(cycle_id) if cycle_id else None,
        "symbol": str(symbol) if symbol else None,
        "shared_context": context if isinstance(context, dict) else None,
        "symbol_block": symbol_block,
        "prompt_chars": len(text),
        "prompt_sha256": _text_hash(text),
    }


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    malformed = 0
    if not path.is_file():
        return rows, malformed
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(value, dict):
                value["_line_number"] = line_number
                rows.append(value)
            else:
                malformed += 1
    return rows, malformed


def _lineage(
    status: str,
    method: str,
    *,
    reason: str | None = None,
    matches: int | None = None,
) -> dict[str, Any]:
    """Return one stable, machine-readable lineage verdict."""

    if status not in _LINEAGE_STATUSES:
        raise ValueError(f"invalid lineage status: {status}")
    payload: dict[str, Any] = {"status": status, "method": method}
    if reason is not None:
        payload["reason"] = reason
    if matches is not None:
        payload["matches"] = int(matches)
    return payload


def _mapping_copy(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _read_dated_ledgers(base_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """Read canonical YYYY-MM-DD JSONL ledgers, never mutable latest projections."""

    rows: list[dict[str, Any]] = []
    malformed = 0
    if not base_dir.is_dir():
        return rows, malformed
    for path in sorted(base_dir.glob("????-??-??.jsonl")):
        current, bad = _read_jsonl(path)
        for row in current:
            row["_source_file"] = str(path)
        rows.extend(current)
        malformed += bad
    return rows, malformed


def _mandate_ref(value: Any) -> dict[str, Any] | None:
    raw = _mapping_copy(value)
    if raw is None:
        return None
    normalized = {field: _clean_text(raw.get(field)) for field in _MANDATE_REF_FIELDS}
    if not normalized["mandate_id"] and not normalized["candidate_scope_id"]:
        return None
    return normalized


def _decision_mandate_ref(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    direct = _mandate_ref(row.get("mandate_ref"))
    if direct is not None:
        return direct
    nested = row.get("decision")
    return _mandate_ref(nested.get("mandate_ref")) if isinstance(nested, Mapping) else None


def _mandate_context(value: Any) -> dict[str, Any] | None:
    raw = _mapping_copy(value)
    if raw is None:
        return None
    if isinstance(raw.get("mandate_ref"), Mapping) or isinstance(raw.get("symbol_mandate"), Mapping):
        return raw
    return None


def _ref_agreement(refs: Sequence[dict[str, Any] | None]) -> bool | None:
    materialized = [ref for ref in refs if ref is not None]
    if len(materialized) < 2:
        return None
    return len({_canonical_hash(ref) for ref in materialized}) == 1


def _scrub_text(value: str, *, repo_root: Path, session_dir: Path, limit: int = 320) -> str:
    scrubbed = value
    replacements = (
        (str(session_dir), "$SESSION"),
        (str(repo_root), "$REPO"),
        (str(Path.home()), "$HOME"),
    )
    for source, target in replacements:
        scrubbed = scrubbed.replace(source, target)
    scrubbed = " ".join(scrubbed.split())
    return scrubbed if len(scrubbed) <= limit else scrubbed[: limit - 1] + "…"


def _native_category(name: str, arguments: dict[str, Any]) -> str:
    if name == "read_file":
        target = str(arguments.get("target_file") or "")
        if target.endswith("prompt_0.txt"):
            return "prompt_file_read"
        if "/bundled/skills/" in target:
            return "skill_read"
        return "file_read"
    if name == "search_tool":
        return "domain_tool_lookup"
    if name in {"search_replace"}:
        return "scratch_mutation"
    if name == "grep":
        return "prompt_search" if "prompt_0.txt" in str(arguments) else "file_search"
    if name == "run_terminal_command":
        command = str(arguments.get("command") or "")
        compact = " ".join(command.split()).lower()
        if compact in {"echo skip", "echo noop", "true", ":"} or 'description":"noop' in compact:
            return "noop"
        if "prompt_0.txt" in command or "/tmp/ctx.json" in command:
            return "prompt_shell_parse"
        if "python" in compact and ("json" in compact or "decimal" in compact):
            return "deterministic_calculation"
        return "terminal_other"
    return "native_other"


def _native_arguments(
    name: str,
    raw_arguments: Any,
    *,
    repo_root: Path,
    session_dir: Path,
) -> dict[str, Any]:
    parsed = _json_load(raw_arguments)
    arguments = parsed if isinstance(parsed, dict) else {}
    normalized: dict[str, Any] = {
        "argument_keys": sorted(str(key) for key in arguments),
        "raw_sha256": _text_hash(str(raw_arguments)),
    }
    if name == "run_terminal_command":
        command = str(arguments.get("command") or "")
        executable = None
        try:
            tokens = shlex.split(command, posix=True)
            executable = tokens[0] if tokens else None
        except ValueError:
            executable = None
        normalized.update(
            {
                "command_chars": len(command),
                "command_sha256": _text_hash(command),
                "command_preview": _scrub_text(command, repo_root=repo_root, session_dir=session_dir),
                "executable": executable,
            }
        )
    elif name == "read_file":
        target = str(arguments.get("target_file") or "")
        normalized.update(
            {
                "target_name": Path(target).name if target else None,
                "target_sha256": _text_hash(target) if target else None,
                "target_preview": _scrub_text(target, repo_root=repo_root, session_dir=session_dir),
                "offset": arguments.get("offset"),
                "limit": arguments.get("limit"),
            }
        )
    elif name == "search_tool":
        normalized.update(
            {
                "query": _scrub_text(
                    str(arguments.get("query") or ""),
                    repo_root=repo_root,
                    session_dir=session_dir,
                    limit=240,
                ),
                "limit": arguments.get("limit"),
            }
        )
    elif name == "grep":
        normalized.update(
            {
                "pattern": _scrub_text(
                    str(arguments.get("pattern") or ""),
                    repo_root=repo_root,
                    session_dir=session_dir,
                    limit=240,
                ),
                "path_preview": _scrub_text(
                    str(arguments.get("path") or ""),
                    repo_root=repo_root,
                    session_dir=session_dir,
                ),
            }
        )
    elif name == "search_replace":
        normalized.update(
            {
                "file_preview": _scrub_text(
                    str(arguments.get("file_path") or ""),
                    repo_root=repo_root,
                    session_dir=session_dir,
                ),
                "old_chars": len(str(arguments.get("old_string") or "")),
                "new_chars": len(str(arguments.get("new_string") or "")),
            }
        )
    return normalized | {"category": _native_category(name, arguments)}


def _tool_result_summary(
    text: str,
    *,
    repo_root: Path,
    session_dir: Path,
    event_outcome: str | None,
) -> dict[str, Any]:
    lowered = text.lower()
    if event_outcome:
        status = "ok" if event_outcome == "success" else "error"
    elif text.startswith("exit: 0"):
        status = "ok"
    elif "failed" in lowered or "error" in lowered or text.startswith("exit: 1"):
        status = "error"
    else:
        status = "unknown"
    return {
        "status": status,
        "chars": len(text),
        "sha256": _text_hash(text),
        "preview": _scrub_text(text, repo_root=repo_root, session_dir=session_dir, limit=240),
    }


def _event_metadata(path: Path) -> tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    completed: dict[str, dict[str, Any]] = {}
    turns: dict[int, dict[str, Any]] = defaultdict(dict)
    open_turns: list[int] = []
    rows, _malformed = _read_jsonl(path)
    for row in rows:
        event_type = row.get("type")
        if event_type == "tool_completed" and row.get("tool_call_id"):
            completed[str(row["tool_call_id"])] = {
                "completed_at": row.get("ts"),
                "duration_ms": row.get("duration_ms"),
                "event_outcome": row.get("outcome"),
                "event_tool_name": row.get("tool_name"),
            }
        elif event_type == "turn_started":
            try:
                turn_number = int(row.get("turn_number"))
            except (TypeError, ValueError):
                continue
            turns[turn_number]["started_at"] = row.get("ts")
            open_turns.append(turn_number)
        elif event_type == "turn_ended":
            explicit_turn_number = row.get("turn_number")
            if explicit_turn_number is not None:
                try:
                    turn_number = int(explicit_turn_number)
                except (TypeError, ValueError):
                    continue
                if turn_number in open_turns:
                    open_turns.remove(turn_number)
            elif open_turns:
                turn_number = open_turns.pop()
            else:
                continue
            turns[turn_number]["ended_at"] = row.get("ts")
            turns[turn_number]["turn_outcome"] = row.get("outcome")
    return completed, dict(turns)


def parse_grok_session(session_dir: Path) -> dict[str, Any]:
    """Parse one raw Grok session without mutating it."""

    summary_path = session_dir / "summary.json"
    chat_path = session_dir / "chat_history.jsonl"
    signals_path = session_dir / "signals.json"
    prompt_path = session_dir / "prompts" / "prompt_0.txt"
    events_path = session_dir / "events.jsonl"

    summary = _json_load(summary_path.read_text(encoding="utf-8", errors="replace")) if summary_path.is_file() else None
    summary = summary if isinstance(summary, dict) else {}
    signals = _json_load(signals_path.read_text(encoding="utf-8", errors="replace")) if signals_path.is_file() else None
    signals = signals if isinstance(signals, dict) else {}
    prompt = _prompt_identity(prompt_path)
    messages, malformed_chat_rows = _read_jsonl(chat_path)
    completed_events, turn_times = _event_metadata(events_path)
    assistant_model_counts = Counter(
        str(row["model_id"]) for row in messages if row.get("type") == "assistant" and row.get("model_id")
    )

    result_by_call_id: dict[str, str] = {}
    for row in messages:
        if row.get("type") == "tool_result" and row.get("tool_call_id"):
            result_by_call_id[str(row["tool_call_id"])] = _content_text(row.get("content"))

    repo_root_raw = summary.get("git_root_dir")
    repo_root = Path(str(repo_root_raw)).resolve() if repo_root_raw else session_dir.resolve()
    session_id = str((summary.get("info") or {}).get("id") or session_dir.name)
    native_steps: list[dict[str, Any]] = []
    domain_requests: dict[str, list[dict[str, Any]]] = defaultdict(list)
    domain_steps: list[dict[str, Any]] = []
    turns: dict[int, dict[str, Any]] = {}
    current_prompt_index: int | None = None
    round_index = 0
    native_ordinal = 0

    for row in messages:
        row_type = row.get("type")
        if row_type == "user" and row.get("prompt_index") is not None:
            try:
                current_prompt_index = int(row["prompt_index"])
            except (TypeError, ValueError):
                current_prompt_index = None
            if current_prompt_index is not None:
                text = _content_text(row.get("content"))
                turn = turns.setdefault(
                    current_prompt_index,
                    {
                        "prompt_index": current_prompt_index,
                        "input_kind": "initial" if current_prompt_index == 0 else "continuation",
                        "input_sha256": _text_hash(text),
                        "input_chars": len(text),
                        "output_kind": None,
                        "output_parse_status": "missing",
                        "output_raw": None,
                    },
                )
                result_payload = _extract_json(text, marker=_DOMAIN_RESULTS_MARKER)
                if isinstance(result_payload, list):
                    turn["input_kind"] = "domain_results"
                    for symbol_block in result_payload:
                        if not isinstance(symbol_block, dict):
                            continue
                        for tool_result in symbol_block.get("tool_results") or []:
                            if not isinstance(tool_result, dict):
                                continue
                            call_id = str(tool_result.get("id") or "")
                            response = {
                                "session_id": session_id,
                                "prompt_index": current_prompt_index,
                                "symbol": symbol_block.get("symbol"),
                                "local_call_id": call_id or None,
                                "tool": tool_result.get("tool"),
                                "transport_ok": tool_result.get("ok"),
                                "result": tool_result.get("result"),
                                "error": tool_result.get("error"),
                            }
                            result = response.get("result")
                            if isinstance(result, dict):
                                response["semantic_valid"] = result.get("valid")
                                rejection_codes: list[str] = []
                                for reason in result.get("reasons") or []:
                                    if isinstance(reason, str):
                                        rejection_codes.append(reason)
                                    elif isinstance(reason, dict):
                                        code = reason.get("code") or reason.get("reason")
                                        if code:
                                            rejection_codes.append(str(code))
                                response["rejection_codes"] = rejection_codes
                                response["evaluation_id"] = result.get("evaluation_id")
                            else:
                                response["semantic_valid"] = None
                                response["rejection_codes"] = []
                                response["evaluation_id"] = None
                            candidates = domain_requests.get(call_id) or []
                            if candidates:
                                candidates[-1]["response"] = response
                            else:
                                domain_steps.append(
                                    {
                                        "session_id": session_id,
                                        "prompt_index": current_prompt_index,
                                        "round_index": None,
                                        "symbol": symbol_block.get("symbol"),
                                        "local_call_id": call_id or None,
                                        "tool": tool_result.get("tool"),
                                        "args": None,
                                        "response": response,
                                        "join_status": "response_without_request",
                                    }
                                )
            continue

        if row_type != "assistant":
            continue

        for call in row.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "unknown")
            native_ordinal += 1
            event = completed_events.get(call_id) or {}
            result_text = result_by_call_id.get(call_id, "")
            native_steps.append(
                {
                    "session_id": session_id,
                    "prompt_index": current_prompt_index,
                    "ordinal": native_ordinal,
                    "native_call_id": call_id or None,
                    "tool_name": name,
                    "arguments": _native_arguments(
                        name,
                        call.get("arguments"),
                        repo_root=repo_root,
                        session_dir=session_dir,
                    ),
                    "result": _tool_result_summary(
                        result_text,
                        repo_root=repo_root,
                        session_dir=session_dir,
                        event_outcome=event.get("event_outcome"),
                    ),
                    **event,
                    "chat_line": row.get("_line_number"),
                }
            )

        text = _content_text(row.get("content"))
        if not text.strip() or current_prompt_index is None:
            continue
        payload = _assistant_payload(text)
        turn = turns.setdefault(
            current_prompt_index,
            {
                "prompt_index": current_prompt_index,
                "input_kind": "unknown",
                "input_sha256": None,
                "input_chars": None,
            },
        )
        if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
            round_index += 1
            turn.update(
                {
                    "output_kind": "domain_tool_request",
                    "output_parse_status": "parsed",
                    "output_raw": text,
                }
            )
            for tool_call in payload["tool_calls"]:
                if not isinstance(tool_call, dict):
                    continue
                call_id = str(tool_call.get("id") or "")
                step = {
                    "session_id": session_id,
                    "prompt_index": current_prompt_index,
                    "round_index": round_index,
                    "symbol": (tool_call.get("args") or {}).get("symbol")
                    if isinstance(tool_call.get("args"), dict)
                    else None,
                    "local_call_id": call_id or None,
                    "tool": tool_call.get("tool"),
                    "args": tool_call.get("args"),
                    "response": None,
                    "join_status": "request_without_response",
                }
                domain_steps.append(step)
                domain_requests[call_id].append(step)
        elif isinstance(payload, dict) and (
            isinstance(payload.get("decisions"), list) or isinstance(payload.get("decision"), dict)
        ):
            turn.update(
                {
                    "output_kind": "decision",
                    "output_parse_status": "parsed",
                    "output_raw": text,
                }
            )
        elif isinstance(payload, dict):
            turn.update(
                {
                    "output_kind": "other_json",
                    "output_parse_status": "parsed",
                    "output_raw": text,
                }
            )
        elif not row.get("tool_calls"):
            # Commentary while native tools are running is not an ACP response.
            turn.setdefault("commentary_outputs", []).append(
                {
                    "sha256": _text_hash(text),
                    "chars": len(text),
                    "preview": _scrub_text(text, repo_root=repo_root, session_dir=session_dir, limit=240),
                }
            )

    for step in domain_steps:
        if (
            step.get("join_status") == "request_without_response"
            and step.get("local_call_id")
            and step.get("response") is not None
        ):
            step["join_status"] = "matched_by_local_call_id"

    for prompt_index, timing in turn_times.items():
        turns.setdefault(prompt_index, {"prompt_index": prompt_index}).update(timing)

    ordered_turns = [turns[key] for key in sorted(turns)]
    try:
        authoritative_turn_count = int(signals.get("turnCount"))
    except (TypeError, ValueError):
        authoritative_turn_count = None
    if authoritative_turn_count is not None and authoritative_turn_count >= 0:
        authoritative_turns = [
            turn for turn in ordered_turns if int(turn.get("prompt_index", -1)) < authoritative_turn_count
        ]
    else:
        authoritative_turns = ordered_turns

    return {
        "session_id": session_id,
        "session_dir": str(session_dir),
        "request_id": summary.get("request_id"),
        "created_at": summary.get("created_at"),
        "updated_at": summary.get("updated_at"),
        "summary_model_id": summary.get("current_model_id"),
        "agent_name": summary.get("agent_name"),
        "reasoning_effort": summary.get("reasoning_effort"),
        "head_commit": summary.get("head_commit"),
        "head_branch": summary.get("head_branch"),
        "prompt": prompt,
        "signals": signals,
        "assistant_model_counts": dict(sorted(assistant_model_counts.items())),
        "malformed_chat_rows": malformed_chat_rows,
        "turns": authoritative_turns,
        "discarded_non_authoritative_turn_count": len(ordered_turns) - len(authoritative_turns),
        "native_steps": native_steps,
        "domain_steps": domain_steps,
    }


def _load_tasks(config: SpikeConfig) -> list[dict[str, Any]]:
    task_db = config.state_dir / "task_ledger.db"
    if not task_db.is_file():
        return []
    conditions = ["kind = 'decide'"]
    params: list[Any] = []
    if config.task_id_min is not None:
        conditions.append("id >= ?")
        params.append(config.task_id_min)
    if config.task_id_max is not None:
        conditions.append("id <= ?")
        params.append(config.task_id_max)
    query = "SELECT * FROM tasks WHERE " + " AND ".join(conditions) + " ORDER BY id"
    with _sqlite_ro(task_db) as connection:
        rows = connection.execute(query, params).fetchall()
    tasks: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_load(row["payload"])
        result = _json_load(row["result"])
        if not isinstance(payload, dict):
            continue
        created_at = _epoch_ms(row["created_at"])
        updated_at = _epoch_ms(row["updated_at"])
        cycle_id = payload.get("cycle_id")
        try:
            cycle_dt = _parse_iso(str(cycle_id)) if cycle_id else created_at
        except (TypeError, ValueError):
            cycle_dt = created_at
        if config.since is not None and cycle_dt is not None and cycle_dt < config.since:
            continue
        if config.until is not None and cycle_dt is not None and cycle_dt > config.until:
            continue
        tasks.append(
            {
                "id": int(row["id"]),
                "kind": row["kind"],
                "dedup_key": row["dedup_key"],
                "partition_key": row["partition_key"],
                "resource": row["resource"],
                "status": row["status"],
                "attempts": int(row["attempts"] or 0),
                "max_attempts": int(row["max_attempts"] or 0),
                "created_at": _iso(created_at),
                "updated_at": _iso(updated_at),
                "error": row["error"],
                "payload": payload,
                "result": result if isinstance(result, dict) else None,
                "cycle_id": str(cycle_id) if cycle_id else None,
                "cycle_dt": cycle_dt,
                "symbol": str(payload.get("symbol") or row["partition_key"] or ""),
            }
        )
    return tasks


def _load_decisions(path: Path) -> tuple[list[dict[str, Any]], int]:
    return _read_jsonl(path)


def _process_id_from_decision(row: dict[str, Any]) -> str | None:
    for container in (row.get("process"), row.get("decision")):
        if isinstance(container, dict) and container.get("process_instance_id"):
            return str(container["process_instance_id"])
    return None


def _parse_daemon_log(path: Path) -> dict[int, dict[str, list[dict[str, Any]]]]:
    by_task: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"acpx_calls": [], "retries": []})
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            acpx = _ACPX_CALL_RE.search(line)
            if acpx:
                task_id = int(acpx.group("task_id"))
                by_task[task_id]["acpx_calls"].append(
                    {
                        "line": line_number,
                        "slot": int(acpx.group("slot")),
                        "symbol": acpx.group("symbol"),
                        "provider": acpx.group("provider"),
                        "timeout_s": float(acpx.group("timeout")),
                        "duration_s": float(acpx.group("duration")),
                        "outcome": acpx.group("outcome"),
                    }
                )
                continue
            retry = _QUEUE_RETRY_RE.search(line)
            if retry:
                task_id = int(retry.group("task_id"))
                by_task[task_id]["retries"].append(
                    {
                        "line": line_number,
                        "symbol": retry.group("symbol"),
                        "attempt": int(retry.group("attempt")),
                        "max_attempts": int(retry.group("max_attempts")),
                        "error": retry.group("error").strip(),
                    }
                )
    return dict(by_task)


def _load_sessions_for_keys(
    sessions_dir: Path,
    task_keys: set[tuple[str, str]],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, int]]:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    counters = Counter()
    if not sessions_dir.is_dir():
        return {}, dict(counters)
    for summary_path in sessions_dir.rglob("summary.json"):
        counters["summaries_scanned"] += 1
        session_dir = summary_path.parent
        prompt = _prompt_identity(session_dir / "prompts" / "prompt_0.txt")
        if prompt.get("status") != "ok":
            counters[f"prompt_{prompt.get('status') or 'unknown'}"] += 1
            continue
        key = (str(prompt["cycle_id"]), str(prompt["symbol"]))
        if key not in task_keys:
            continue
        session = parse_grok_session(session_dir)
        by_key[key].append(session)
        counters["sessions_in_scope"] += 1
    for sessions in by_key.values():
        sessions.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("session_id") or ""),
            )
        )
    return dict(by_key), dict(counters)


def _query_rows_by_values(
    connection: sqlite3.Connection,
    query_prefix: str,
    values: Iterable[str],
) -> list[sqlite3.Row]:
    unique = sorted({str(value) for value in values if value})
    rows: list[sqlite3.Row] = []
    for offset in range(0, len(unique), 500):
        chunk = unique[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(connection.execute(query_prefix + f" ({placeholders})", chunk).fetchall())
    return rows


def _load_effects(
    casys_db: Path,
    process_ids: Iterable[str],
    decision_ids: Iterable[str],
) -> tuple[
    dict[tuple[str, str | None], list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    process_by_attempt: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    fills_by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not casys_db.is_file():
        return {}, {}
    with _sqlite_ro(casys_db) as connection:
        events = _query_rows_by_values(
            connection,
            "SELECT * FROM process_events WHERE process_instance_id IN",
            process_ids,
        )
        fills = _query_rows_by_values(
            connection,
            "SELECT * FROM broker_fills WHERE decision_id IN",
            decision_ids,
        )
    for row in events:
        item = dict(row)
        item["caused_by"] = _json_load(item.pop("caused_by_json", None))
        item["effect_refs"] = _json_load(item.pop("effect_refs_json", None))
        item["version_pins"] = _json_load(item.pop("version_pins_json", None))
        attempt_id = str(row["attempt_id"]) if row["attempt_id"] else None
        process_by_attempt[(str(row["process_instance_id"]), attempt_id)].append(item)
    for rows in process_by_attempt.values():
        rows.sort(key=lambda item: int(item.get("seq") or 0))
    for row in fills:
        fills_by_decision[str(row["decision_id"])].append(dict(row))
    for rows in fills_by_decision.values():
        rows.sort(key=lambda item: int(item.get("seq") or 0))
    return dict(process_by_attempt), dict(fills_by_decision)


def _load_learning_outcomes(
    learnings_db: Path,
    decision_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    if not learnings_db.is_file():
        return {}
    try:
        with _sqlite_ro(learnings_db) as connection:
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(notes)")
            }
            optional = (
                "evaluation_basis",
                "horizon_used",
                "evaluated_at",
                "source_cycle_id",
            )
            optional_projection = ", ".join(
                field if field in columns else f"NULL AS {field}"
                for field in optional
            )
            rows = _query_rows_by_values(
                connection,
                f"""SELECT decision_id, ts, symbol, action, intent, executed, reason,
                           verdict, forward_return, outcome_score, q_value, q_updates,
                           outcome_semantics_version, {optional_projection}
                    FROM notes WHERE decision_id IN""",
                decision_ids,
            )
    except sqlite3.Error:
        return {}
    return {str(row["decision_id"]): dict(row) for row in rows}


def _load_universe_sources(state_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    history_path = state_dir / "universe_mandates" / "history.jsonl"
    mandates, malformed_mandates = _read_jsonl(history_path)
    for row in mandates:
        row["_source_file"] = str(history_path)
    runs, malformed_runs = _read_dated_ledgers(state_dir / "universe_runs")
    scopes, malformed_scopes = _read_dated_ledgers(state_dir / "candidate_scopes")

    mandates_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    runs_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    runs_by_mandate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scopes_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in mandates:
        if mandate_id := _clean_text(row.get("mandate_id")):
            mandates_by_id[mandate_id].append(row)
    for row in runs:
        if run_id := _clean_text(row.get("agent_run_id")):
            runs_by_id[run_id].append(row)
        prepared_ref = row.get("mandate_prepared_ref")
        if isinstance(prepared_ref, Mapping):
            if mandate_id := _clean_text(prepared_ref.get("mandate_id")):
                runs_by_mandate[mandate_id].append(row)
    for row in scopes:
        if scope_id := _clean_text(row.get("candidate_scope_id")):
            scopes_by_id[scope_id].append(row)

    return (
        {
            "mandates_by_id": dict(mandates_by_id),
            "runs_by_id": dict(runs_by_id),
            "runs_by_mandate": dict(runs_by_mandate),
            "scopes_by_id": dict(scopes_by_id),
        },
        {
            "mandate_history_rows": len(mandates),
            "mandate_history_malformed": malformed_mandates,
            "universe_run_rows": len(runs),
            "universe_run_malformed": malformed_runs,
            "candidate_scope_rows": len(scopes),
            "candidate_scope_malformed": malformed_scopes,
        },
    )


def _load_universe_selection_outcomes(
    casys_db: Path,
    mandate_ids: Iterable[str],
) -> tuple[
    dict[tuple[str, str, str], list[dict[str, Any]]],
    dict[tuple[str, str], list[dict[str, Any]]],
    dict[str, Any],
]:
    exact: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    broad: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    if not casys_db.is_file():
        return {}, {}, {"status": "absent", "reason": "casys_db_missing", "rows": 0}
    try:
        with _sqlite_ro(casys_db) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe_selection_outcomes'"
            ).fetchone()
            if exists is None:
                return (
                    {},
                    {},
                    {
                        "status": "absent",
                        "reason": "universe_selection_outcomes_table_missing",
                        "rows": 0,
                    },
                )
            rows = _query_rows_by_values(
                connection,
                "SELECT * FROM universe_selection_outcomes WHERE mandate_id IN",
                mandate_ids,
            )
            metadata_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe_selection_metadata'"
            ).fetchone()
            semantics_version = None
            if metadata_exists is not None:
                metadata_row = connection.execute(
                    "SELECT value FROM universe_selection_metadata WHERE key='selection_semantics_version'"
                ).fetchone()
                semantics_version = str(metadata_row[0]) if metadata_row is not None else None
    except sqlite3.Error as exc:
        return (
            {},
            {},
            {
                "status": "absent",
                "reason": "universe_selection_outcomes_unreadable",
                "detail": type(exc).__name__,
                "rows": 0,
            },
        )

    for row in rows:
        item = dict(row)
        if "allowed_sides" in item:
            parsed_sides = _json_load(item.get("allowed_sides"))
            item["allowed_sides"] = parsed_sides if isinstance(parsed_sides, list) else []
        mandate_id = _clean_text(item.get("mandate_id"))
        symbol = _clean_text(item.get("symbol"))
        as_of = _clean_text(item.get("as_of"))
        exact[(mandate_id, symbol, as_of)].append(item)
        broad[(mandate_id, symbol)].append(item)
    for grouped in (exact, broad):
        for values in grouped.values():
            values.sort(
                key=lambda item: (
                    int(item.get("horizon_sessions") or 0),
                    _clean_text(item.get("verdict_basis")) or "direction",
                    int(item.get("id") or 0),
                )
            )
    try:
        from trader.domain.universe.selection_attribution import SELECTION_SEMANTICS_VERSION
    except ImportError:
        SELECTION_SEMANTICS_VERSION = "bench_v3"
    semantics_trusted = semantics_version == SELECTION_SEMANTICS_VERSION
    return (
        dict(exact),
        dict(broad),
        {
            "status": "available" if semantics_trusted else "stale_or_unversioned",
            "reason": None if semantics_trusted else "selection_semantics_version_mismatch_or_missing",
            "rows": len(rows),
            "semantics_version": semantics_version,
            "expected_semantics_version": SELECTION_SEMANTICS_VERSION,
            "semantics_trusted": semantics_trusted,
        },
    )


def _load_execution_cycles(
    casys_db: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Build fee-aware flat-to-flat cycles from the canonical broker ledger."""

    if not casys_db.is_file():
        return {}, {"status": "absent", "reason": "casys_db_missing", "fills": 0, "cycles": 0}
    try:
        with _sqlite_ro(casys_db) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_fills'"
            ).fetchone()
            if exists is None:
                return {}, {
                    "status": "absent",
                    "reason": "broker_fills_table_missing",
                    "fills": 0,
                    "cycles": 0,
                }
            fills = [dict(row) for row in connection.execute("SELECT * FROM broker_fills ORDER BY seq")]
    except sqlite3.Error as exc:
        return {}, {
            "status": "absent",
            "reason": "broker_fills_unreadable",
            "detail": type(exc).__name__,
            "fills": 0,
            "cycles": 0,
        }

    try:
        from trader.reporting.read_models.trade_history import (
            aggregate_position_cycles,
            compute_round_trips,
        )

        cycles = aggregate_position_cycles(compute_round_trips(fills=fills))
    except (ImportError, TypeError, ValueError) as exc:
        return {}, {
            "status": "absent",
            "reason": "canonical_position_cycle_reconstruction_failed",
            "detail": type(exc).__name__,
            "fills": len(fills),
            "cycles": 0,
        }

    by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cycle in cycles:
        raw_ids = cycle.get("entry_decision_ids")
        entry_ids = raw_ids if isinstance(raw_ids, list) else []
        if not entry_ids and cycle.get("entry_decision_id"):
            entry_ids = [cycle["entry_decision_id"]]
        for raw_id in entry_ids:
            if decision_id := _clean_text(raw_id):
                by_decision[decision_id].append(dict(cycle))
    return dict(by_decision), {
        "status": "available",
        "reason": None,
        "fills": len(fills),
        "cycles": len(cycles),
    }


def _compact_shared_context(shared: dict[str, Any], symbol: str) -> dict[str, Any]:
    portfolio = shared.get("portfolio") if isinstance(shared.get("portfolio"), dict) else {}
    holdings = portfolio.get("holdings") if isinstance(portfolio.get("holdings"), list) else []
    target_holding = next(
        (holding for holding in holdings if isinstance(holding, dict) and str(holding.get("symbol")) == symbol),
        None,
    )
    portfolio_summary = {
        key: portfolio.get(key)
        for key in (
            "cash",
            "cash_ledger",
            "cash_available",
            "equity",
            "total_return_pct",
            "long_exposure_usd",
            "short_exposure_usd",
            "gross_exposure_usd",
            "net_exposure_usd",
        )
        if key in portfolio
    }
    risk_capacity = shared.get("risk_capacity") if isinstance(shared.get("risk_capacity"), dict) else {}
    per_symbol = risk_capacity.get("per_symbol") if isinstance(risk_capacity.get("per_symbol"), dict) else {}
    risk_projection = {key: value for key, value in risk_capacity.items() if key != "per_symbol"}
    if symbol in per_symbol:
        risk_projection["target"] = per_symbol[symbol]

    cockpit = shared.get("cockpit") if isinstance(shared.get("cockpit"), dict) else {}
    focus = cockpit.get("focus") if isinstance(cockpit.get("focus"), dict) else {}
    focus_cols = focus.get("cols") if isinstance(focus.get("cols"), list) else []
    focus_rows = focus.get("rows") if isinstance(focus.get("rows"), list) else []
    target_focus = next(
        (row for row in focus_rows if isinstance(row, list) and row and str(row[0]) == symbol),
        None,
    )
    return {
        "now": shared.get("now"),
        "market_clocks": shared.get("market_clocks"),
        "stale_market_data": shared.get("stale_market_data"),
        "active_plans_summary": shared.get("active_plans_summary"),
        "portfolio": portfolio_summary | {"target_holding": target_holding},
        "risk_limits": shared.get("risk_limits"),
        "risk_capacity": risk_projection,
        "cockpit_focus": {"cols": focus_cols, "target": target_focus},
    }


def _decision_projection(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
    return {
        "decision_id": row.get("decision_id"),
        "cycle_ts": row.get("cycle_ts"),
        "sequence": row.get("sequence"),
        "symbol": row.get("symbol"),
        "decision_source": row.get("decision_source"),
        "model_called": row.get("model_called"),
        "action": row.get("action"),
        "intent": row.get("intent"),
        "confidence": row.get("confidence"),
        "decision_reason_code": row.get("decision_reason_code"),
        "executed": row.get("executed"),
        "reason": row.get("reason"),
        "llm_provider": row.get("llm_provider"),
        "llm_model": row.get("llm_model"),
        "experiment_id": row.get("experiment_id"),
        "experiment_components": row.get("experiment_components"),
        "experiment_status": row.get("experiment_status"),
        "code_version": row.get("code_version"),
        "rationale": row.get("rationale") or decision.get("rationale"),
        "opportunity_side": row.get("opportunity_side") or decision.get("opportunity_side"),
        "thesis": row.get("thesis") if isinstance(row.get("thesis"), dict) else decision.get("thesis"),
        "entry_dimensions": row.get("entry_dimensions"),
        "mandate_ref": _decision_mandate_ref(row),
        "trade_evaluation_id": row.get("trade_evaluation_id"),
        "trade_plan_evaluation": row.get("trade_plan_evaluation"),
        "domain_tools": {
            "tool_rounds": decision.get("tool_rounds"),
            "tool_calls": decision.get("tool_calls"),
        },
        "market_snapshot": row.get("market_snapshot"),
        "portfolio_snapshot": row.get("portfolio_snapshot"),
        "runtime": row.get("runtime"),
    }


def _mandate_snapshot_projection(row: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    symbols = row.get("symbols") if isinstance(row.get("symbols"), Mapping) else {}
    symbol_mandate = symbols.get(symbol) if isinstance(symbols, Mapping) else None
    return {
        **{field: row.get(field) for field in _MANDATE_REF_FIELDS},
        "valid_until": row.get("valid_until"),
        "fallback_reason": row.get("fallback_reason"),
        "symbol_mandate": dict(symbol_mandate) if isinstance(symbol_mandate, Mapping) else None,
        "family_postures": row.get("family_postures"),
        "portfolio_posture": row.get("portfolio_posture"),
        "source": {
            "file": row.get("_source_file"),
            "line": row.get("_line_number"),
        },
    }


def _universe_run_projection(row: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    symbol_mandates = row.get("symbol_mandates") if isinstance(row.get("symbol_mandates"), Mapping) else {}
    symbol_rationales = row.get("symbol_rationales") if isinstance(row.get("symbol_rationales"), Mapping) else {}
    request_snapshot = row.get("request_snapshot")
    request_snapshot = dict(request_snapshot) if isinstance(request_snapshot, Mapping) else None
    if request_snapshot is None:
        point_in_time_selection_feedback = {
            "status": "absent_legacy_run",
            "reason": "request_payload_not_persisted_only_hash_available",
            "payload": None,
        }
    elif "selection_feedback" in request_snapshot:
        point_in_time_selection_feedback = {
            "status": "observed",
            "reason": None,
            "payload": request_snapshot.get("selection_feedback"),
        }
    else:
        point_in_time_selection_feedback = {
            "status": "available_empty",
            "reason": "no_selection_feedback_was_present_in_the_request",
            "payload": {},
        }
    return {
        "agent_run_id": row.get("agent_run_id"),
        "candidate_scope_id": row.get("candidate_scope_id"),
        "parent_candidate_scope_id": row.get("parent_candidate_scope_id"),
        "venue": row.get("venue"),
        "as_of": row.get("as_of"),
        "scope_as_of": row.get("scope_as_of"),
        "scope_phase": row.get("scope_phase"),
        "status": row.get("status"),
        "agent_provider": row.get("agent_provider"),
        "agent_model": row.get("agent_model"),
        "agent_provider_fallback_reason": row.get("agent_provider_fallback_reason"),
        "request_payload_hash": row.get("request_payload_hash"),
        "request_snapshot": request_snapshot,
        "prompt_observation": row.get("prompt_observation"),
        "tool_trace": row.get("tool_trace"),
        "input_signature": row.get("input_signature"),
        "latency_ms": row.get("latency_ms"),
        "refresh_reason": row.get("refresh_reason"),
        "selected_hotlist": row.get("selected_hotlist"),
        "baseline": row.get("baseline"),
        "summary": row.get("summary"),
        "family_postures": row.get("family_postures"),
        "market_context": row.get("market_context"),
        "global_family_board": row.get("global_family_board"),
        "global_situation_digest": row.get("global_situation_digest"),
        "global_universe_posture": row.get("global_universe_posture"),
        "target_symbol_rationale": symbol_rationales.get(symbol),
        "target_symbol_mandate": symbol_mandates.get(symbol),
        "mandate_prepared_ref": row.get("mandate_prepared_ref"),
        "source": {
            "file": row.get("_source_file"),
            "line": row.get("_line_number"),
        },
        "point_in_time_selection_feedback": point_in_time_selection_feedback,
    }


def _candidate_scope_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_scope_id": row.get("candidate_scope_id"),
        "parent_candidate_scope_id": row.get("parent_candidate_scope_id"),
        "venue": row.get("venue"),
        "as_of": row.get("as_of"),
        "candidate_scope_as_of": row.get("candidate_scope_as_of"),
        "scope_phase": row.get("scope_phase"),
        "candidates": row.get("candidates"),
        "candidate_run_ids": row.get("candidate_run_ids"),
        "default_hotlist": row.get("default_hotlist"),
        "hotlist": row.get("hotlist"),
        "sticky_context_at_close": row.get("sticky_context_at_close"),
        "scores": row.get("scores"),
        "stale": row.get("stale"),
        "source": {
            "file": row.get("_source_file"),
            "line": row.get("_line_number"),
        },
    }


def _row_active_before(row: Mapping[str, Any], cycle_id: str | None) -> bool:
    if cycle_id is None:
        return True
    try:
        row_ts = _parse_iso(_clean_text(row.get("as_of")))
        cycle_ts = _parse_iso(cycle_id)
    except ValueError:
        return False
    return row_ts <= cycle_ts


def _resolve_universe_lineage(
    *,
    symbol: str,
    cycle_id: str | None,
    task_facts: Mapping[str, Any],
    model_symbol_block: Mapping[str, Any] | None,
    decision_row: Mapping[str, Any] | None,
    sources: Mapping[str, Any],
    selection_exact: Mapping[tuple[str, str, str], list[dict[str, Any]]],
    selection_broad: Mapping[tuple[str, str], list[dict[str, Any]]],
    selection_source_quality: Mapping[str, Any],
) -> dict[str, Any]:
    model_context = _mandate_context(
        model_symbol_block.get("universe_mandate") if isinstance(model_symbol_block, Mapping) else None
    )
    task_context = _mandate_context(task_facts.get("universe_mandate"))
    model_ref = _mandate_ref((model_context or {}).get("mandate_ref"))
    task_ref = _mandate_ref((task_context or {}).get("mandate_ref"))
    decision_ref = _decision_mandate_ref(decision_row)
    if model_ref is not None:
        chosen_ref = model_ref
        observed_context = model_context
        ref_lineage = _lineage("exact", "model_observation.universe_mandate.mandate_ref")
    elif task_ref is not None:
        chosen_ref = task_ref
        observed_context = task_context
        ref_lineage = _lineage(
            "reconstructed",
            "task_payload.per_symbol_facts.universe_mandate.mandate_ref",
            reason="raw_model_observation_missing_mandate_ref",
        )
    elif decision_ref is not None:
        chosen_ref = decision_ref
        observed_context = None
        ref_lineage = _lineage(
            "reconstructed",
            "decision_row.mandate_ref",
            reason="pre_decision_mandate_context_missing",
        )
    else:
        chosen_ref = None
        observed_context = model_context or task_context
        ref_lineage = _lineage("absent", "no_mandate_ref", reason="unmandated_or_trace_missing")

    mandate_row: dict[str, Any] | None = None
    mandate_lineage = _lineage("absent", "mandate_history", reason="mandate_ref_absent")
    if chosen_ref is not None:
        mandate_id = _clean_text(chosen_ref.get("mandate_id"))
        candidates = [
            row for row in sources.get("mandates_by_id", {}).get(mandate_id, []) if _row_active_before(row, cycle_id)
        ]
        exact_ref_ready = all(
            _clean_text(chosen_ref.get(field))
            for field in ("mandate_id", "candidate_scope_id", "venue", "as_of", "status")
        ) and (bool(_clean_text(chosen_ref.get("agent_run_id"))) or _clean_text(chosen_ref.get("status")) == "fallback")
        exact_candidates = []
        if exact_ref_ready:
            exact_candidates = [
                row
                for row in candidates
                if all(
                    _clean_text(row.get(field)) == _clean_text(chosen_ref.get(field)) for field in _MANDATE_REF_FIELDS
                )
                and isinstance(row.get("symbols"), Mapping)
                and symbol in row["symbols"]
            ]
        if len(exact_candidates) == 1:
            mandate_row = exact_candidates[0]
            mandate_lineage = _lineage(
                "exact",
                "unique_mandate_ref_full_identity+symbol",
                matches=1,
            )
        elif len(exact_candidates) > 1:
            mandate_lineage = _lineage(
                "absent",
                "unique_mandate_ref_full_identity+symbol",
                reason="ambiguous_duplicate_full_identity_history_rows",
                matches=len(exact_candidates),
            )
        else:
            activated = [
                row
                for row in candidates
                if _clean_text(row.get("status")) in {"active", "fallback"}
                and isinstance(row.get("symbols"), Mapping)
                and symbol in row["symbols"]
            ]
            if len(activated) == 1:
                mandate_row = activated[0]
                mandate_lineage = _lineage(
                    "reconstructed",
                    "mandate_id+symbol+activated_before_cycle",
                    reason="full_mandate_ref_not_available_or_not_matched",
                    matches=1,
                )
            elif activated:
                mandate_lineage = _lineage(
                    "absent",
                    "mandate_id+symbol+activated_before_cycle",
                    reason="ambiguous_multiple_activated_mandates",
                    matches=len(activated),
                )
            else:
                mandate_lineage = _lineage(
                    "absent",
                    "mandate_history",
                    reason="activated_mandate_snapshot_not_found",
                    matches=len(candidates),
                )

    run_row: dict[str, Any] | None = None
    run_lineage = _lineage("absent", "universe_runs", reason="agent_run_id_absent")
    chosen_run_id = _clean_text((chosen_ref or {}).get("agent_run_id"))
    recovered_run_id = _clean_text((mandate_row or {}).get("agent_run_id"))
    if chosen_run_id:
        run_candidates = [
            row for row in sources.get("runs_by_id", {}).get(chosen_run_id, []) if _row_active_before(row, cycle_id)
        ]
        if len(run_candidates) == 1:
            run_row = run_candidates[0]
            run_lineage = _lineage("exact", "unique_mandate_ref.agent_run_id", matches=1)
        elif run_candidates:
            run_lineage = _lineage(
                "absent",
                "unique_mandate_ref.agent_run_id",
                reason="ambiguous_duplicate_agent_run_id_rows",
                matches=len(run_candidates),
            )
        else:
            run_lineage = _lineage("absent", "mandate_ref.agent_run_id", reason="universe_run_not_found")
    elif recovered_run_id:
        run_candidates = [
            row for row in sources.get("runs_by_id", {}).get(recovered_run_id, []) if _row_active_before(row, cycle_id)
        ]
        if len(run_candidates) == 1:
            run_row = run_candidates[0]
            run_lineage = _lineage(
                "reconstructed",
                "mandate_history.agent_run_id",
                reason="agent_run_id_missing_from_observed_ref",
                matches=len(run_candidates),
            )
        elif run_candidates:
            run_lineage = _lineage(
                "absent",
                "mandate_history.agent_run_id",
                reason="ambiguous_duplicate_agent_run_id_rows",
                matches=len(run_candidates),
            )
    elif chosen_ref is not None and _clean_text(chosen_ref.get("mandate_id")):
        run_candidates = list(sources.get("runs_by_mandate", {}).get(_clean_text(chosen_ref.get("mandate_id")), []))
        run_candidates = [row for row in run_candidates if _row_active_before(row, cycle_id)]
        if len(run_candidates) == 1:
            run_row = run_candidates[0]
            run_lineage = _lineage(
                "reconstructed",
                "universe_run.mandate_prepared_ref.mandate_id",
                reason="agent_run_id_missing",
                matches=len(run_candidates),
            )
        elif run_candidates:
            run_lineage = _lineage(
                "absent",
                "universe_run.mandate_prepared_ref.mandate_id",
                reason="ambiguous_multiple_universe_runs_for_mandate",
                matches=len(run_candidates),
            )

    scope_row: dict[str, Any] | None = None
    chosen_scope_id = _clean_text((chosen_ref or {}).get("candidate_scope_id"))
    recovered_scope_id = _clean_text((mandate_row or {}).get("candidate_scope_id")) or _clean_text(
        (run_row or {}).get("candidate_scope_id")
    )
    scope_id = chosen_scope_id or recovered_scope_id
    if scope_id:
        scope_candidates = [
            row for row in sources.get("scopes_by_id", {}).get(scope_id, []) if _row_active_before(row, cycle_id)
        ]
        if len(scope_candidates) == 1:
            scope_row = scope_candidates[0]
            scope_lineage = _lineage(
                "exact" if chosen_scope_id else "reconstructed",
                ("unique_mandate_ref.candidate_scope_id" if chosen_scope_id else "unique_recovered_candidate_scope_id"),
                reason=None if chosen_scope_id else "candidate_scope_id_missing_from_observed_ref",
                matches=1,
            )
        elif scope_candidates:
            scope_lineage = _lineage(
                "absent",
                "unique_candidate_scopes.candidate_scope_id",
                reason="ambiguous_duplicate_candidate_scope_id_rows",
                matches=len(scope_candidates),
            )
        else:
            scope_lineage = _lineage(
                "absent",
                "candidate_scopes.candidate_scope_id",
                reason="candidate_scope_snapshot_not_found",
            )
    else:
        scope_lineage = _lineage("absent", "candidate_scopes", reason="candidate_scope_id_absent")

    selected_as_of = _clean_text((mandate_row or {}).get("as_of")) or _clean_text((chosen_ref or {}).get("as_of"))
    mandate_id = _clean_text((chosen_ref or {}).get("mandate_id")) or _clean_text((mandate_row or {}).get("mandate_id"))
    selection_rows = [
        row
        for row in selection_exact.get((mandate_id, symbol, selected_as_of), [])
        if _row_active_before(row, cycle_id)
    ]
    if selection_rows:
        selection_lineage = _lineage(
            "exact",
            "mandate_id+symbol+activation_as_of",
            matches=len(selection_rows),
        )
    else:
        broad_rows = (
            [row for row in selection_broad.get((mandate_id, symbol), []) if _row_active_before(row, cycle_id)]
            if mandate_id
            else []
        )
        broad_as_of = {_clean_text(row.get("as_of")) for row in broad_rows}
        if broad_rows and len(broad_as_of) == 1:
            selection_rows = broad_rows
            selection_lineage = _lineage(
                "reconstructed",
                "mandate_id+symbol",
                reason="activation_as_of_missing_or_not_matched",
                matches=len(selection_rows),
            )
        elif broad_rows:
            selection_rows = []
            selection_lineage = _lineage(
                "absent",
                "mandate_id+symbol",
                reason="ambiguous_multiple_activation_as_of",
                matches=len(broad_rows),
            )
        else:
            selection_lineage = _lineage(
                "absent",
                "universe_selection_outcomes",
                reason="selection_outcome_pending_or_not_evaluated",
            )

    observed_symbol_mandate = (
        (observed_context or {}).get("symbol_mandate")
        if isinstance((observed_context or {}).get("symbol_mandate"), Mapping)
        else None
    )
    if observed_symbol_mandate is None and mandate_row is not None:
        symbols = mandate_row.get("symbols") if isinstance(mandate_row.get("symbols"), Mapping) else {}
        recovered = symbols.get(symbol) if isinstance(symbols, Mapping) else None
        observed_symbol_mandate = dict(recovered) if isinstance(recovered, Mapping) else None

    return {
        "mandate_ref": chosen_ref,
        "observed_context": observed_context,
        "observed_symbol_mandate": observed_symbol_mandate,
        "mandate_snapshot": (_mandate_snapshot_projection(mandate_row, symbol) if mandate_row is not None else None),
        "universe_run": _universe_run_projection(run_row, symbol) if run_row is not None else None,
        "candidate_scope": _candidate_scope_projection(scope_row) if scope_row is not None else None,
        "selection_outcomes": selection_rows,
        "selection_source_quality": dict(selection_source_quality),
        "refs": {
            "model_observation": model_ref,
            "task_payload": task_ref,
            "decision_row": decision_ref,
            "all_available_refs_agree": _ref_agreement((model_ref, task_ref, decision_ref)),
        },
        "lineage": {
            "observed_mandate_ref": ref_lineage,
            "mandate_activation": mandate_lineage,
            "universe_run": run_lineage,
            "candidate_scope": scope_lineage,
            "universe_selection_flair": selection_lineage,
            "decision_mandate_ref": (
                _lineage("exact", "decision_row.mandate_ref")
                if decision_ref is not None and chosen_ref == decision_ref
                else _lineage(
                    "reconstructed" if decision_ref is not None else "absent",
                    "decision_row.mandate_ref",
                    reason=(
                        "decision_ref_differs_from_observed_ref" if decision_ref is not None else "decision_ref_missing"
                    ),
                )
            ),
        },
    }


def _execution_target(
    *,
    decision_id: str | None,
    immediate_fills: Sequence[Mapping[str, Any]],
    cycles_by_decision: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    cycles = list(cycles_by_decision.get(decision_id, [])) if decision_id else []
    projected_cycles: list[dict[str, Any]] = []
    shared_attribution = False
    for cycle in cycles:
        entry_ids = [_clean_text(value) for value in (cycle.get("entry_decision_ids") or []) if _clean_text(value)]
        if len(entry_ids) != 1:
            shared_attribution = True
        quality = cycle.get("commission_quality") if isinstance(cycle.get("commission_quality"), Mapping) else {}
        deployed = cycle.get("entry_notional_usd")
        net_pnl = cycle.get("pnl")
        try:
            parsed_deployed = float(deployed)
            parsed_pnl = float(net_pnl)
            economics_available = (
                quality.get("status") == "available"
                and math.isfinite(parsed_deployed)
                and parsed_deployed > 0.0
                and math.isfinite(parsed_pnl)
            )
            net_return = parsed_pnl / parsed_deployed if economics_available else None
            if net_return is not None and not math.isfinite(net_return):
                net_return = None
                economics_available = False
        except (TypeError, ValueError, ZeroDivisionError):
            net_return = None
            economics_available = False
        projected_cycles.append(
            {
                **dict(cycle),
                "net_return": net_return,
                "outcome_eligible": economics_available,
            }
        )
    if cycles:
        lineage = _lineage(
            "exact",
            "canonical_broker_fills.flat_to_flat.entry_decision_ids",
            matches=len(cycles),
        )
    else:
        lineage = _lineage(
            "absent",
            "canonical_broker_fills.flat_to_flat.entry_decision_ids",
            reason="position_not_opened_or_cycle_not_closed_or_decision_id_missing",
        )
    return {
        "immediate_fills": [dict(row) for row in immediate_fills],
        "position_cycles": projected_cycles,
        "has_execution_fill": bool(immediate_fills),
        "has_realised_cycle": bool(projected_cycles),
        "fee_complete": bool(projected_cycles) and all(row.get("outcome_eligible") is True for row in projected_cycles),
        "attribution_grain": ("shared_position_cycle" if shared_attribution else "single_entry_decision"),
        "lineage": lineage,
        "semantics": "canonical_broker_flat_to_flat_position_cycle",
    }


def _trader_flair_eligibility(
    decision_row: Mapping[str, Any] | None,
    learning_outcome: Mapping[str, Any] | None,
) -> dict[str, str]:
    if decision_row is None:
        return {"status": "missing_trace", "reason": "decision_missing"}
    if decision_row.get("decision_source") != "llm" or decision_row.get("model_called") is not True:
        return {"status": "not_applicable", "reason": "not_an_authentic_llm_decision"}
    try:
        from trader.application.record.decision_recorder import candidate_learning_note

        candidate, _rationale, _annotation = candidate_learning_note(dict(decision_row))
    except ImportError:
        candidate = None
    if candidate:
        return (
            {"status": "eligible", "reason": "authentic_llm_learning_note_present"}
            if learning_outcome is not None
            else {"status": "missing_trace", "reason": "eligible_learning_note_missing"}
        )
    action = _clean_text(decision_row.get("action")).upper()
    intent = _clean_text(decision_row.get("intent")).upper()
    opportunity_side = _clean_text(decision_row.get("opportunity_side")).lower()
    learning = _clean_text(decision_row.get("learning"))
    if (action == "HOLD" or intent == "HOLD") and opportunity_side not in {"long", "short"} and not learning:
        return {
            "status": "not_applicable",
            "reason": "undirected_hold_not_selected_for_learning",
        }
    return {
        "status": "not_applicable",
        "reason": (
            "learning_note_without_eligible_llm_decision"
            if learning_outcome is not None
            else "llm_rationale_not_exploitable"
        ),
    }


def _trader_flair_target(
    *,
    decision_row: Mapping[str, Any] | None,
    learning_outcome: Mapping[str, Any] | None,
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    eligibility = _trader_flair_eligibility(decision_row, learning_outcome)
    if learning_outcome is None:
        outcome_lineage = _lineage(
            "absent",
            "learnings.notes.decision_id",
            reason="learning_note_or_feedback_missing",
        )
        verdict = None
        semantics_version = None
        trusted = False
        maturity = eligibility["status"]
    else:
        outcome_lineage = _lineage("exact", "learnings.notes.decision_id")
        verdict = _clean_text(learning_outcome.get("verdict")).upper() or None
        semantics_version = learning_outcome.get("outcome_semantics_version")
        try:
            from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION
        except ImportError:
            BENCHMARK_SEMANTICS_VERSION = 3
        # A newly persisted note normally has neither verdict nor semantics
        # version until the delayed judge matures it.  That is pending data,
        # not stale evaluated data.
        if verdict is None:
            trusted = False
            maturity = "pending"
        else:
            trusted = semantics_version == BENCHMARK_SEMANTICS_VERSION
            if not trusted:
                maturity = "stale_semantics"
            elif verdict == "UNKNOWN":
                maturity = "evaluated_unknown"
            else:
                maturity = "mature"

    if eligibility["status"] != "eligible":
        maturity = eligibility["status"]

    persisted_evaluation_basis = _clean_text(
        (learning_outcome or {}).get("evaluation_basis")
    ).lower()
    persisted_horizon = _clean_text((learning_outcome or {}).get("horizon_used")) or None
    persisted_evaluated_at = _clean_text((learning_outcome or {}).get("evaluated_at")) or None
    persisted_source_cycle_id = _clean_text(
        (learning_outcome or {}).get("source_cycle_id")
    ) or None
    intent = _clean_text((decision_row or {}).get("intent")).upper()
    decision_id = _clean_text((decision_row or {}).get("decision_id"))
    realised_policy = (
        bool((decision_row or {}).get("executed"))
        and intent in _OPENING_INTENTS
        and not decision_id.startswith("synth:")
    )
    realised_consistency: dict[str, Any] = {"status": "not_applicable"}
    realised_label_consistent = False
    if decision_row is None:
        realised_available = False
        basis = "not_applicable"
        basis_status = "decision_missing"
        benchmark_context = None
        horizon = None
    elif realised_policy:
        basis = "realised_flat_to_flat_fee_complete"
        realised_available = bool(execution.get("has_realised_cycle") and execution.get("fee_complete"))
        basis_status = "available" if realised_available else "pending_or_fee_incomplete"
        benchmark_context = None
        horizon = persisted_horizon or "position_cycle"
        eligible_cycles = [
            row
            for row in (execution.get("position_cycles") or [])
            if isinstance(row, Mapping) and row.get("outcome_eligible") is True
        ]
        if not realised_available:
            realised_consistency = {"status": "pending_execution"}
        elif len(eligible_cycles) != 1:
            realised_consistency = {
                "status": "inconsistent",
                "reason": "expected_one_fee_complete_position_cycle",
                "cycle_count": len(eligible_cycles),
            }
            basis_status = "ambiguous_multiple_position_cycles"
            if trusted and verdict is not None and eligibility["status"] == "eligible":
                maturity = "inconsistent_trace"
        elif not trusted or verdict is None:
            realised_consistency = {"status": "pending_or_untrusted_label"}
        else:
            canonical_return = eligible_cycles[0].get("net_return")
            try:
                observed_return = float(learning_outcome.get("forward_return")) if learning_outcome else math.nan
                canonical_value = float(canonical_return)
                return_matches = math.isfinite(observed_return) and math.isclose(
                    observed_return,
                    canonical_value,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                )
            except (TypeError, ValueError):
                observed_return = math.nan
                canonical_value = math.nan
                return_matches = False
            try:
                from trader.application.record.learning_outcomes import realised_verdict

                expected_verdict, _expected_reward = realised_verdict(canonical_value)
            except (ImportError, ValueError):
                expected_verdict = None
            verdict_matches = expected_verdict == verdict
            realised_label_consistent = bool(return_matches and verdict_matches)
            realised_consistency = {
                "status": "verified" if realised_label_consistent else "inconsistent",
                "canonical_net_return": canonical_return,
                "learning_forward_return": (learning_outcome.get("forward_return") if learning_outcome else None),
                "expected_verdict": expected_verdict,
                "learning_verdict": verdict,
            }
            if not realised_label_consistent:
                maturity = "inconsistent_trace"
                basis_status = "inconsistent_with_canonical_execution"
        if trusted and verdict is not None and not realised_available and eligibility["status"] == "eligible":
            maturity = "inconsistent_trace"
        elif not realised_available and maturity not in {
            "stale_semantics",
            "missing_trace",
            "not_applicable",
        }:
            maturity = "pending"
    else:
        realised_available = False
        basis = "counterfactual_market_1d_or_fallback_4h"
        basis_status = "evaluated" if trusted and verdict is not None else "pending"
        horizon = persisted_horizon or "legacy_1d_then_4h_exact_choice_unavailable"
        try:
            from trader.domain.decision_benchmark import decision_benchmark_context

            benchmark_context = decision_benchmark_context(decision_row) if decision_row is not None else None
        except ImportError:
            benchmark_context = None

    if eligibility["status"] != "eligible":
        maturity = eligibility["status"]

    target_trusted = (
        trusted and eligibility["status"] == "eligible" and (not realised_policy or realised_label_consistent)
    )
    trainable_label = bool(target_trusted and verdict in _VERDICT_REWARD)
    return {
        "status": "evaluated" if maturity in {"mature", "evaluated_unknown"} else maturity,
        "maturity": maturity,
        "verdict": verdict if target_trusted else None,
        "reward": _VERDICT_REWARD.get(verdict) if target_trusted and verdict is not None else None,
        "forward_return": (learning_outcome.get("forward_return") if target_trusted and learning_outcome else None),
        "outcome_score": (learning_outcome.get("outcome_score") if target_trusted and learning_outcome else None),
        "trainable_label": trainable_label,
        "flair_score_status": (
            "complete"
            if trainable_label and learning_outcome and learning_outcome.get("outcome_score") is not None
            else "pending"
            if trainable_label
            else "not_trainable"
        ),
        "eligibility": eligibility,
        "basis": basis,
        "evaluation_basis": persisted_evaluation_basis or None,
        "basis_status": basis_status,
        "realised_consistency": realised_consistency,
        "horizon": horizon,
        "evaluated_at": persisted_evaluated_at,
        "source_cycle_id": persisted_source_cycle_id,
        "benchmark_context": benchmark_context,
        "outcome_semantics_version": semantics_version,
        "score_semantics": "FLAIR_outcome_score_separate_from_reward_and_memrl_q",
        "outcome_score_training_policy": (
            "diagnostic_only_as_persisted; recompute_shrinkage_and_lift_inside_each_training_fold"
        ),
        "lineage": outcome_lineage,
    }


def _workflow_projection(
    *,
    attempts: Sequence[Mapping[str, Any]],
    turns: Sequence[Mapping[str, Any]],
    native_steps: Sequence[Mapping[str, Any]],
    domain_steps: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "attempts": [dict(row) for row in attempts],
        "turns": [
            {
                key: row.get(key)
                for key in (
                    "queue_attempt_no",
                    "session_id",
                    "prompt_index",
                    "input_kind",
                    "output_kind",
                    "output_parse_status",
                    "started_at",
                    "ended_at",
                    "turn_outcome",
                )
            }
            for row in turns
        ],
        "native_steps": [
            {
                "queue_attempt_no": row.get("queue_attempt_no"),
                "session_id": row.get("session_id"),
                "prompt_index": row.get("prompt_index"),
                "ordinal": row.get("ordinal"),
                "tool": row.get("tool_name"),
                "category": (row.get("arguments") or {}).get("category"),
                "outcome": row.get("event_outcome") or (row.get("result") or {}).get("status"),
            }
            for row in native_steps
        ],
        "domain_steps": [
            {
                "queue_attempt_no": row.get("queue_attempt_no"),
                "session_id": row.get("session_id"),
                "prompt_index": row.get("prompt_index"),
                "round_index": row.get("round_index"),
                "tool": row.get("tool"),
                "join_status": row.get("join_status"),
                "transport_ok": (row.get("response") or {}).get("transport_ok"),
                "semantic_valid": (row.get("response") or {}).get("semantic_valid"),
                "rejection_codes": (row.get("response") or {}).get("rejection_codes") or [],
            }
            for row in domain_steps
        ],
        # ACP turns, native CLI events and domain-tool calls come from separate
        # ledgers.  Keep their internal order, but never invent an interleaving
        # that the persisted trace cannot prove.
        "event_tokens_by_channel": {
            "turns": [
                f"turn:{row.get('input_kind') or 'missing'}->{row.get('output_kind') or 'missing'}:"
                f"{row.get('turn_outcome') or 'missing'}"
                for row in turns
            ],
            "native_cli": [
                f"native:{(row.get('arguments') or {}).get('category') or row.get('tool_name') or 'missing'}:"
                f"{row.get('event_outcome') or (row.get('result') or {}).get('status') or 'missing'}"
                for row in native_steps
            ],
            "domain_tools": [
                f"domain:{row.get('tool') or 'missing'}:"
                f"{(row.get('response') or {}).get('semantic_valid') if row.get('response') else 'missing'}"
                for row in domain_steps
            ],
        },
        "cross_channel_order": "not_persisted",
    }


def _universe_flair_target(
    *,
    selection_rows: Sequence[Mapping[str, Any]],
    selection_quality: Mapping[str, Any],
    symbol_mandate: Mapping[str, Any],
    lineage: Mapping[str, Any],
    mandate_status: str | None = None,
) -> dict[str, Any]:
    try:
        from trader.domain.universe.selection_attribution import DEFAULT_FORWARD_SESSIONS
    except ImportError:
        DEFAULT_FORWARD_SESSIONS = 5
    canonical_rows = [
        row for row in selection_rows if int(row.get("horizon_sessions") or 0) == DEFAULT_FORWARD_SESSIONS
    ]
    agent_rows = [row for row in canonical_rows if _clean_text(row.get("selector")) in {"", "agent"}]
    fallback_rows = [row for row in canonical_rows if _clean_text(row.get("selector")) not in {"", "agent"}]

    def by_basis(rows: Sequence[Mapping[str, Any]], basis: str) -> list[Mapping[str, Any]]:
        return [row for row in rows if (_clean_text(row.get("verdict_basis")) or "direction") == basis]

    normalized_sides = {
        _clean_text(side).lower()
        for side in (symbol_mandate.get("allowed_sides") or [])
        if _clean_text(side).lower() in {"long", "short"}
    }
    direction_applicable = len(normalized_sides) == 1
    normalized_status = _clean_text(mandate_status).lower()
    agent_applicable = normalized_status != "fallback"
    fallback_applicable = normalized_status == "fallback" or bool(fallback_rows)

    def universe_basis(
        rows: Sequence[Mapping[str, Any]],
        *,
        applicable: bool = True,
    ) -> dict[str, Any]:
        if not applicable:
            status = "not_applicable"
        elif not selection_quality.get("semantics_trusted"):
            status = "stale_or_unversioned"
        elif not rows:
            status = "absent_or_pending"
        elif any(row.get("flair_score") is None for row in rows):
            status = "verdict_evaluated_flair_pending"
        else:
            status = "evaluated"
        return {"status": status, "outcomes": [dict(row) for row in rows]}

    allocation_rows = by_basis(agent_rows, "allocation")
    direction_rows = by_basis(agent_rows, "direction")
    return {
        "status": universe_basis(allocation_rows, applicable=agent_applicable)["status"],
        "primary_cohort": {
            "selector": "agent",
            "horizon_sessions": DEFAULT_FORWARD_SESSIONS,
            "verdict_basis": "allocation",
        },
        "primary_basis": "allocation",
        "allocation": universe_basis(allocation_rows, applicable=agent_applicable),
        "direction": universe_basis(
            direction_rows,
            applicable=agent_applicable and direction_applicable,
        ),
        "baseline_fallback": {
            "allocation": universe_basis(
                by_basis(fallback_rows, "allocation"),
                applicable=fallback_applicable,
            ),
            "direction": universe_basis(
                by_basis(fallback_rows, "direction"),
                applicable=fallback_applicable and direction_applicable,
            ),
            "semantics": "control_cohort_not_agent_target",
        },
        "other_horizons": [
            dict(row) for row in selection_rows if int(row.get("horizon_sessions") or 0) != DEFAULT_FORWARD_SESSIONS
        ],
        "other_outcomes": [
            dict(row)
            for row in agent_rows
            if (_clean_text(row.get("verdict_basis")) or "direction") not in {"allocation", "direction"}
        ],
        "source_quality": dict(selection_quality),
        "lineage": dict(lineage),
        "semantics": (
            "allocation_selection_quality_is_primary; direction_is_separate; neither_is_trader_decision_quality"
        ),
    }


def _build_cross_loop_episode(
    *,
    trajectory: Mapping[str, Any],
    decision_row: Mapping[str, Any] | None,
    universe: Mapping[str, Any],
    task_turns: Sequence[Mapping[str, Any]],
    task_native_steps: Sequence[Mapping[str, Any]],
    task_domain_steps: Sequence[Mapping[str, Any]],
    execution: Mapping[str, Any],
    trader_flair: Mapping[str, Any],
) -> dict[str, Any]:
    identity = dict(trajectory["identity"])
    decision = trajectory.get("decision") if isinstance(trajectory.get("decision"), Mapping) else {}
    model_observation = (
        trajectory.get("model_observation") if isinstance(trajectory.get("model_observation"), Mapping) else {}
    )
    symbol_block = (
        model_observation.get("symbol_block") if isinstance(model_observation.get("symbol_block"), Mapping) else {}
    )
    market_symbol_state = dict(symbol_block)
    market_symbol_state.pop("universe_mandate", None)
    symbol_mandate = universe.get("observed_symbol_mandate")
    symbol_mandate = symbol_mandate if isinstance(symbol_mandate, Mapping) else {}
    tpe_results = [
        {
            "queue_attempt_no": row.get("queue_attempt_no"),
            "round_index": row.get("round_index"),
            "args": row.get("args"),
            "transport_ok": (row.get("response") or {}).get("transport_ok"),
            "semantic_valid": (row.get("response") or {}).get("semantic_valid"),
            "rejection_codes": (row.get("response") or {}).get("rejection_codes") or [],
            "evaluation_id": (row.get("response") or {}).get("evaluation_id"),
            "result": (row.get("response") or {}).get("result"),
        }
        for row in task_domain_steps
        if row.get("tool") == "evaluate_trade_plan"
    ]
    structured_thesis = decision.get("thesis") if isinstance(decision.get("thesis"), Mapping) else None
    selection_quality = universe.get("selection_source_quality")
    selection_quality = selection_quality if isinstance(selection_quality, Mapping) else {}
    selection_rows = list(universe.get("selection_outcomes") or [])
    mandate_status = _clean_text((universe.get("mandate_ref") or {}).get("status"))
    universe_flair = _universe_flair_target(
        selection_rows=selection_rows,
        selection_quality=selection_quality,
        symbol_mandate=symbol_mandate,
        lineage=universe["lineage"]["universe_selection_flair"],
        mandate_status=mandate_status,
    )
    workflow = _workflow_projection(
        attempts=trajectory.get("attempts") or [],
        turns=task_turns,
        native_steps=task_native_steps,
        domain_steps=task_domain_steps,
    )
    observation_source = model_observation.get("source") if isinstance(model_observation.get("source"), Mapping) else {}
    selected_attempt = next(
        (
            attempt
            for attempt in trajectory.get("attempts") or []
            if attempt.get("session_id") == observation_source.get("session_id")
        ),
        {},
    )
    observed_universe_context = universe.get("observed_context")
    has_predecision_mandate = isinstance(observed_universe_context, Mapping)
    successful_observation = observation_source.get("selection") == "successful_attempt"
    complete_session_join = (trajectory.get("join_quality") or {}).get("raw_sessions") == "exact_business_key_and_count"
    action_training_eligible = bool(model_observation and decision and successful_observation and complete_session_join)
    if action_training_eligible:
        action_training_reason = "successful_attempt_prompt+exact_session_count+decision"
    elif not decision:
        action_training_reason = "decision_missing"
    elif not successful_observation:
        action_training_reason = "successful_attempt_session_or_prompt_missing"
    elif not complete_session_join:
        action_training_reason = "raw_session_count_mismatch"
    else:
        action_training_reason = "model_observation_missing"
    action_ex_ante = {
        "configuration": {
            key: selected_attempt.get(key)
            for key in (
                "summary_model_id",
                "agent_name",
                "reasoning_effort",
                "head_commit",
            )
        },
        "market_state": {
            "source": {key: observation_source.get(key) for key in ("session_id", "queue_attempt_no", "file")},
            "state_hash": model_observation.get("state_hash"),
            "shared_projection": model_observation.get("shared_projection"),
            "symbol_observation": market_symbol_state,
            "consistent_across_attempts": model_observation.get("consistent_across_attempts"),
        },
        "universe": {
            "observed_context": (dict(observed_universe_context) if has_predecision_mandate else None),
            "mandate_snapshot": universe.get("mandate_snapshot") if has_predecision_mandate else None,
            "universe_run": universe.get("universe_run") if has_predecision_mandate else None,
            "candidate_scope": universe.get("candidate_scope") if has_predecision_mandate else None,
            "hypothesis": {
                "why_selected": symbol_mandate.get("why_selected"),
                "directional_view": symbol_mandate.get("directional_view"),
                "allowed_sides": symbol_mandate.get("allowed_sides"),
                "posture": symbol_mandate.get("posture"),
                "role": symbol_mandate.get("role"),
                "confidence": symbol_mandate.get("confidence"),
            },
            "ablation_eligibility": ("eligible" if has_predecision_mandate else "mandate_not_observed_predecision"),
        },
    }
    return {
        "schema_version": CROSS_LOOP_SCHEMA_VERSION,
        "episode_id": f"brain:{identity.get('task_id')}:{identity.get('cycle_id')}:{identity.get('symbol')}",
        "identity": identity,
        "configuration": {
            "experiment_id": decision.get("experiment_id"),
            "experiment_components": decision.get("experiment_components"),
            "experiment_status": decision.get("experiment_status"),
            "code_version": decision.get("code_version"),
            "attempt_models": [
                {
                    key: attempt.get(key)
                    for key in (
                        "queue_attempt_no",
                        "summary_model_id",
                        "assistant_model_counts",
                        "agent_name",
                        "reasoning_effort",
                        "head_commit",
                    )
                }
                for attempt in trajectory.get("attempts") or []
            ],
        },
        "market_state": {
            "source": model_observation.get("source"),
            "state_hash": model_observation.get("state_hash"),
            "shared_projection": model_observation.get("shared_projection"),
            "symbol_observation": market_symbol_state,
            "observation_is_actual_prompt": bool(model_observation),
        },
        "feature_views": {"action_ex_ante": action_ex_ante},
        "training_eligibility": {
            "action_ex_ante": {
                "status": "eligible" if action_training_eligible else "missing_trace",
                "reason": action_training_reason,
            }
        },
        "universe_context": {
            key: universe.get(key)
            for key in (
                "mandate_ref",
                "observed_context",
                "mandate_snapshot",
                "universe_run",
                "candidate_scope",
                "refs",
            )
        },
        "hypotheses": {
            "universe": {
                "why_selected": symbol_mandate.get("why_selected"),
                "directional_view": symbol_mandate.get("directional_view"),
                "allowed_sides": symbol_mandate.get("allowed_sides"),
                "posture": symbol_mandate.get("posture"),
                "role": symbol_mandate.get("role"),
                "confidence": symbol_mandate.get("confidence"),
            },
            "trader": {
                "structured_thesis": structured_thesis,
                "structured_thesis_status": "present" if structured_thesis is not None else "absent",
                "rationale": decision.get("rationale"),
                "opportunity_side": decision.get("opportunity_side"),
                "confidence": decision.get("confidence"),
                "decision_reason_code": decision.get("decision_reason_code"),
                "entry_dimensions": decision.get("entry_dimensions"),
                "evaluate_trade_plan_results": tpe_results,
            },
        },
        "trader_workflow": workflow,
        "decision": dict(decision) if decision else None,
        "target": {
            "trader_flair": dict(trader_flair),
            "universe_selection_flair": universe_flair,
            "execution": dict(execution),
            "memrl": (
                {
                    "q_value": trajectory["learning_outcome"].get("q_value"),
                    "q_updates": trajectory["learning_outcome"].get("q_updates"),
                }
                if isinstance(trajectory.get("learning_outcome"), Mapping)
                else None
            ),
        },
        "lineage": {
            "task_to_raw_sessions": _lineage(
                "reconstructed" if trajectory.get("attempts") else "absent",
                "cycle_id+symbol+session_created_at_order",
                reason=(None if trajectory.get("attempts") else "raw_sessions_missing"),
                matches=len(trajectory.get("attempts") or []),
            ),
            "task_to_decision": (
                _lineage("exact", "task_dedup_key=cycle_id+symbol")
                if decision
                else _lineage("absent", "task_dedup_key=cycle_id+symbol", reason="decision_missing")
            ),
            **dict(universe.get("lineage") or {}),
            "decision_to_trader_flair": trader_flair["lineage"],
            "decision_to_execution": execution["lineage"],
        },
        "evaluation_contract": {
            "primary_label": "target.trader_flair",
            "trainable_primary_fields": [
                "target.trader_flair.verdict",
                "target.trader_flair.reward",
                "target.trader_flair.forward_return",
            ],
            "persisted_outcome_score_policy": (
                "never_a_global_training_target; recompute_train_fold_FLAIR_lift_and_shrinkage"
            ),
            "auxiliary_label": "target.universe_selection_flair",
            "execution_result": (
                "target.execution_is_an_explanatory_continuous_result; for_modern_openings_its_fee_complete_"
                "net_return_is_the_source_of_trader_flair_not_an_independent_third_reward"
            ),
            "views": {
                "action_ex_ante": {
                    "features": ["feature_views.action_ex_ante"],
                    "label": "decision.action+decision.intent",
                    "excluded": [
                        "hypotheses.trader",
                        "trader_workflow",
                        "decision",
                        "target",
                    ],
                },
                "trader_flair_outcome": {
                    "features": [
                        "configuration",
                        "market_state",
                        "universe_context",
                        "hypotheses",
                        "trader_workflow",
                        "decision",
                    ],
                    "label": "target.trader_flair",
                    "excluded": [
                        "target.universe_selection_flair",
                        "target.execution",
                        "target.memrl",
                    ],
                },
            },
            "anti_leakage": (
                "targets_are_delayed_and_must_never_be_reintroduced_as_same_episode_features; "
                "legacy_universe_runs_without_request_snapshot_keep_selection_feedback_absent; "
                "no_scheduler_event_or_later_task_is_a_market_transition_target"
            ),
        },
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _queue_attempt_outcome(*, task_status: str, expected_attempts: int, attempt_index: int) -> str:
    if attempt_index < expected_attempts:
        return "retry"
    if attempt_index == expected_attempts:
        return "success" if task_status == "done" else "dead"
    return "unexpected_extra_session"


def reconstruct(config: SpikeConfig) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    tasks = _load_tasks(config)
    task_keys = {
        (str(task["cycle_id"]), str(task["symbol"])) for task in tasks if task.get("cycle_id") and task.get("symbol")
    }
    decisions, malformed_decisions = _load_decisions(config.state_dir / "decisions.jsonl")

    decision_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in decisions:
        if row.get("cycle_ts") and row.get("symbol"):
            decision_by_key[(str(row["cycle_ts"]), str(row["symbol"]))].append(row)

    sessions_by_key, session_scan = _load_sessions_for_keys(config.sessions_dir, task_keys)
    daemon = _parse_daemon_log(config.daemon_log)
    universe_sources, universe_source_counts = _load_universe_sources(config.state_dir)

    joined_decisions: dict[int, tuple[dict[str, Any] | None, str]] = {}
    decision_ids: list[str] = []
    process_ids: list[str] = []
    for task in tasks:
        process = task["payload"].get("process")
        process_id = (
            str(process.get("process_instance_id"))
            if isinstance(process, dict) and process.get("process_instance_id")
            else None
        )
        # The task's UNIQUE dedup_key is exactly ``cycle_ts:symbol``.  A
        # process_instance_id is only a cohort/process key: retry/recovery can
        # legitimately reuse it for more than one queue task.
        matches = decision_by_key.get((str(task["cycle_id"]), str(task["symbol"])), [])
        method = "dedup_key=cycle_id+symbol"
        if len(matches) == 1:
            joined_decisions[task["id"]] = (matches[0], method)
            if matches[0].get("decision_id"):
                decision_ids.append(str(matches[0]["decision_id"]))
        else:
            joined_decisions[task["id"]] = (None, "missing" if not matches else "ambiguous")
        if process_id:
            process_ids.append(process_id)

    process_events, fills = _load_effects(config.state_dir / "casys.db", process_ids, decision_ids)
    outcomes = _load_learning_outcomes(config.state_dir / "learnings.db", decision_ids)
    mandate_ids: set[str] = set()
    for task in tasks:
        facts = task["payload"].get("per_symbol_facts")
        context = _mandate_context(facts.get("universe_mandate")) if isinstance(facts, Mapping) else None
        ref = _mandate_ref((context or {}).get("mandate_ref"))
        if ref is not None and ref.get("mandate_id"):
            mandate_ids.add(str(ref["mandate_id"]))
        for session in sessions_by_key.get((str(task["cycle_id"]), str(task["symbol"])), []):
            prompt = session.get("prompt") if isinstance(session.get("prompt"), Mapping) else {}
            symbol_block = prompt.get("symbol_block") if isinstance(prompt.get("symbol_block"), Mapping) else {}
            model_context = _mandate_context(symbol_block.get("universe_mandate"))
            model_ref = _mandate_ref((model_context or {}).get("mandate_ref"))
            if model_ref is not None and model_ref.get("mandate_id"):
                mandate_ids.add(str(model_ref["mandate_id"]))
    for decision, _method in joined_decisions.values():
        ref = _decision_mandate_ref(decision)
        if ref is not None and ref.get("mandate_id"):
            mandate_ids.add(str(ref["mandate_id"]))
    selection_exact, selection_broad, selection_source_quality = _load_universe_selection_outcomes(
        config.state_dir / "casys.db",
        mandate_ids,
    )
    execution_cycles, execution_source_quality = _load_execution_cycles(config.state_dir / "casys.db")

    next_task: dict[int, dict[str, Any]] = {}
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        by_symbol[task["symbol"]].append(task)
    for symbol_tasks in by_symbol.values():
        symbol_tasks.sort(
            key=lambda item: (item.get("cycle_dt") or datetime.min.replace(tzinfo=timezone.utc), item["id"])
        )
        for current, following in zip(symbol_tasks, symbol_tasks[1:]):
            next_task[current["id"]] = following

    trajectories: list[dict[str, Any]] = []
    cross_loop_episodes: list[dict[str, Any]] = []
    native_events: list[dict[str, Any]] = []
    domain_events: list[dict[str, Any]] = []
    turn_events: list[dict[str, Any]] = []
    matched_session_ids: set[str] = set()
    counters = Counter()
    native_tool_counts = Counter()
    native_category_counts = Counter()
    domain_tool_counts = Counter()
    parser_error_counts = Counter()
    summary_model_counts = Counter()
    assistant_model_counts = Counter()

    for task in tasks:
        key = (str(task["cycle_id"]), str(task["symbol"]))
        sessions = list(sessions_by_key.get(key, []))
        log_rows = daemon.get(task["id"], {"acpx_calls": [], "retries": []})
        retries = sorted(log_rows.get("retries", []), key=lambda item: item["attempt"])
        retries_by_attempt = {int(row["attempt"]): row for row in retries if row.get("attempt") is not None}
        attempts: list[dict[str, Any]] = []
        task_native_events: list[dict[str, Any]] = []
        task_domain_events: list[dict[str, Any]] = []
        task_turn_events: list[dict[str, Any]] = []
        prompt_observation_hashes: set[str] = set()
        for attempt_index, session in enumerate(sessions, start=1):
            session_id = str(session["session_id"])
            matched_session_ids.add(session_id)
            queue_outcome = _queue_attempt_outcome(
                task_status=str(task["status"]),
                expected_attempts=int(task["attempts"]),
                attempt_index=attempt_index,
            )
            if queue_outcome == "retry":
                parser_error = (retries_by_attempt.get(attempt_index) or {}).get("error")
            elif queue_outcome == "dead":
                parser_error = task.get("error")
            else:
                parser_error = None
            if parser_error:
                parser_error_counts[str(parser_error)] += 1

            prompt = session.get("prompt") or {}
            if session.get("summary_model_id"):
                summary_model_counts[str(session["summary_model_id"])] += 1
            assistant_model_counts.update(session.get("assistant_model_counts") or {})
            task_shared = task["payload"].get("shared_context")
            task_facts = task["payload"].get("per_symbol_facts")
            prompt_shared = prompt.get("shared_context")
            prompt_symbol_block = prompt.get("symbol_block")
            prompt_facts = dict(prompt_symbol_block) if isinstance(prompt_symbol_block, dict) else None
            if prompt_facts is not None:
                prompt_facts.pop("symbol", None)
            prompt_shared_matches = (
                isinstance(task_shared, dict)
                and isinstance(prompt_shared, dict)
                and _canonical_hash(task_shared) == _canonical_hash(prompt_shared)
            )
            prompt_facts_match = (
                isinstance(task_facts, dict)
                and isinstance(prompt_facts, dict)
                and _canonical_hash(task_facts) == _canonical_hash(prompt_facts)
            )
            prompt_observation_hash = (
                _canonical_hash(
                    {
                        "shared_context": prompt_shared,
                        "symbol_block": prompt_symbol_block,
                    }
                )
                if isinstance(prompt_shared, dict) and isinstance(prompt_symbol_block, dict)
                else None
            )
            if prompt_observation_hash:
                prompt_observation_hashes.add(prompt_observation_hash)
            attempt_ref = {
                "task_id": task["id"],
                "cycle_id": task["cycle_id"],
                "symbol": task["symbol"],
                "queue_attempt_no": attempt_index,
                "session_id": session_id,
            }
            for native in session.get("native_steps") or []:
                event = attempt_ref | native
                native_events.append(event)
                task_native_events.append(event)
                counters["parsed_native_event_calls"] += 1
                if event.get("event_outcome") == "error":
                    counters["parsed_native_event_errors"] += 1
                native_tool_counts[str(event.get("tool_name") or "unknown")] += 1
                category = (event.get("arguments") or {}).get("category")
                native_category_counts[str(category or "unknown")] += 1
            for domain in session.get("domain_steps") or []:
                event = attempt_ref | domain
                domain_events.append(event)
                task_domain_events.append(event)
                request = event.get("request") if isinstance(event.get("request"), dict) else event
                response = event.get("response") if isinstance(event.get("response"), dict) else {}
                domain_tool_counts[str(request.get("tool") or response.get("tool") or "unknown")] += 1
            for turn in session.get("turns") or []:
                event = attempt_ref | turn
                turn_events.append(event)
                task_turn_events.append(event)

            attempts.append(
                {
                    "queue_attempt_no": attempt_index,
                    "queue_outcome": queue_outcome,
                    "parser_error": parser_error,
                    "session_id": session_id,
                    "request_id": session.get("request_id"),
                    "created_at": session.get("created_at"),
                    "updated_at": session.get("updated_at"),
                    "summary_model_id": session.get("summary_model_id"),
                    "assistant_model_counts": session.get("assistant_model_counts"),
                    "agent_name": session.get("agent_name"),
                    "reasoning_effort": session.get("reasoning_effort"),
                    "head_commit": session.get("head_commit"),
                    "prompt_sha256": prompt.get("prompt_sha256"),
                    "prompt_chars": prompt.get("prompt_chars"),
                    "prompt_observation_hash": prompt_observation_hash,
                    "prompt_shared_context_matches_task": prompt_shared_matches,
                    "prompt_symbol_facts_matches_task": prompt_facts_match,
                    "turn_count": (session.get("signals") or {}).get("turnCount"),
                    "assistant_message_count": (session.get("signals") or {}).get("assistantMessageCount"),
                    "native_tool_call_count": (session.get("signals") or {}).get("toolCallCount"),
                    "native_tool_failure_count": (session.get("signals") or {}).get("toolFailureCount"),
                    "malformed_chat_rows": session.get("malformed_chat_rows"),
                    "discarded_non_authoritative_turn_count": session.get("discarded_non_authoritative_turn_count"),
                }
            )

        decision_row, decision_join_method = joined_decisions[task["id"]]
        decision_id = (
            str(decision_row.get("decision_id"))
            if isinstance(decision_row, dict) and decision_row.get("decision_id")
            else None
        )
        decision_process_id = _process_id_from_decision(decision_row) if isinstance(decision_row, dict) else None
        process = task["payload"].get("process")
        process_id = (
            str(process.get("process_instance_id"))
            if isinstance(process, dict) and process.get("process_instance_id")
            else None
        )
        process_attempt_id = (
            str(process.get("attempt_id")) if isinstance(process, dict) and process.get("attempt_id") else None
        )
        if decision_row is None:
            process_identity = "not_applicable_without_decision"
        elif process_id and decision_process_id == process_id:
            process_identity = "exact"
        elif decision_process_id is None:
            process_identity = "missing_in_decision"
        else:
            process_identity = "mismatch"
        shared_context = task["payload"].get("shared_context")
        shared_context = shared_context if isinstance(shared_context, dict) else {}
        facts = task["payload"].get("per_symbol_facts")
        facts = facts if isinstance(facts, dict) else {}
        following = next_task.get(task["id"])
        later_task_observation = None
        if following is not None:
            next_dt = following.get("cycle_dt")
            current_dt = task.get("cycle_dt")
            next_facts = following["payload"].get("per_symbol_facts")
            next_shared = following["payload"].get("shared_context")
            later_task_observation = {
                "task_id": following["id"],
                "cycle_id": following["cycle_id"],
                "delta_t_seconds": round((next_dt - current_dt).total_seconds(), 6)
                if isinstance(next_dt, datetime) and isinstance(current_dt, datetime)
                else None,
                "state_hash": _canonical_hash({"shared_context": next_shared, "per_symbol_facts": next_facts}),
                "per_symbol_facts": next_facts,
                "semantics": "diagnostic_next_due_task_not_market_transition",
            }

        state_before: dict[str, Any] = {
            "source": {"database": "task_ledger.db", "task_id": task["id"]},
            "state_hash": _canonical_hash({"shared_context": shared_context, "per_symbol_facts": facts}),
            "shared_projection": _compact_shared_context(shared_context, task["symbol"]),
            "per_symbol_facts": facts,
        }
        if config.include_shared_context:
            state_before["shared_context"] = shared_context

        observation_index = None
        for index in range(len(attempts) - 1, -1, -1):
            if attempts[index].get("queue_outcome") == "success":
                observation_index = index
                break
        if observation_index is None and sessions:
            observation_index = len(sessions) - 1
        observed_session = sessions[observation_index] if observation_index is not None else {}
        observed_prompt = observed_session.get("prompt")
        observed_prompt = observed_prompt if isinstance(observed_prompt, dict) else {}
        model_shared = observed_prompt.get("shared_context")
        model_symbol_block = observed_prompt.get("symbol_block")
        model_observation = None
        if isinstance(model_shared, dict) and isinstance(model_symbol_block, dict):
            model_observation = {
                "source": {
                    "session_id": observed_session.get("session_id"),
                    "queue_attempt_no": (observation_index + 1 if observation_index is not None else None),
                    "selection": (
                        "successful_attempt"
                        if observation_index is not None
                        and attempts[observation_index].get("queue_outcome") == "success"
                        else "last_available_attempt"
                    ),
                    "file": "prompts/prompt_0.txt",
                },
                "state_hash": _canonical_hash(
                    {
                        "shared_context": model_shared,
                        "symbol_block": model_symbol_block,
                    }
                ),
                "shared_projection": _compact_shared_context(model_shared, task["symbol"]),
                "symbol_block": model_symbol_block,
                "consistent_across_attempts": len(prompt_observation_hashes) == 1,
            }
            if config.include_shared_context:
                model_observation["shared_context"] = model_shared

        process_rows = (
            process_events.get((process_id, process_attempt_id), []) if process_id and process_attempt_id else []
        )
        trajectory = {
            "schema_version": SCHEMA_VERSION,
            "identity": {
                "task_id": task["id"],
                "cycle_id": task["cycle_id"],
                "symbol": task["symbol"],
                "process_instance_id": process_id,
                "process_attempt_id": process_attempt_id,
                "runtime_run_id": process.get("runtime_run_id") if isinstance(process, dict) else None,
                "decision_id": decision_id,
            },
            "queue": {
                "status": task["status"],
                "attempts_total": task["attempts"],
                "max_attempts": task["max_attempts"],
                "resource": task["resource"],
                "dedup_key": task["dedup_key"],
                "partition_key": task["partition_key"],
                "created_at": task["created_at"],
                "updated_at": task["updated_at"],
                "stored_error": task["error"],
                "successful_attempt_model_calls": (task.get("result") or {}).get("model_calls"),
                "daemon_acpx_calls": log_rows.get("acpx_calls", []),
                "retry_history": retries,
            },
            "state_before": state_before,
            "model_observation": model_observation,
            "attempts": attempts,
            "decision": _decision_projection(decision_row),
            "effects": {
                "process_events": process_rows,
                "fills": fills.get(decision_id, []) if decision_id else [],
            },
            "learning_outcome": outcomes.get(decision_id) if decision_id else None,
            "later_task_observation": later_task_observation,
            "join_quality": {
                "decision": decision_join_method,
                "decision_process_identity": process_identity,
                "raw_sessions": (
                    "exact_business_key_and_count" if len(sessions) == task["attempts"] else "count_mismatch"
                ),
                "raw_session_count": len(sessions),
                "expected_attempt_count": task["attempts"],
                "prompt_shared_context_all_match": bool(attempts)
                and all(attempt["prompt_shared_context_matches_task"] for attempt in attempts),
                "prompt_symbol_facts_all_match": bool(attempts)
                and all(attempt["prompt_symbol_facts_matches_task"] for attempt in attempts),
                "prompt_observation_consistent_across_attempts": bool(attempts) and len(prompt_observation_hashes) == 1,
                "process_events": ("exact_process_instance_id+attempt_id" if process_rows else "missing"),
                "learning_outcome": "exact_decision_id" if decision_id in outcomes else "missing",
                "later_task_observation": (
                    "diagnostic_same_symbol_next_due_task" if following is not None else "missing"
                ),
            },
        }
        universe = _resolve_universe_lineage(
            symbol=str(task["symbol"]),
            cycle_id=str(task["cycle_id"]) if task.get("cycle_id") else None,
            task_facts=facts,
            model_symbol_block=(model_symbol_block if isinstance(model_symbol_block, Mapping) else None),
            decision_row=decision_row,
            sources=universe_sources,
            selection_exact=selection_exact,
            selection_broad=selection_broad,
            selection_source_quality=selection_source_quality,
        )
        execution = _execution_target(
            decision_id=decision_id,
            immediate_fills=fills.get(decision_id, []) if decision_id else [],
            cycles_by_decision=execution_cycles,
        )
        learning_outcome = outcomes.get(decision_id) if decision_id else None
        trader_flair = _trader_flair_target(
            decision_row=decision_row,
            learning_outcome=learning_outcome,
            execution=execution,
        )
        episode = _build_cross_loop_episode(
            trajectory=trajectory,
            decision_row=decision_row,
            universe=universe,
            task_turns=task_turn_events,
            task_native_steps=task_native_events,
            task_domain_steps=task_domain_events,
            execution=execution,
            trader_flair=trader_flair,
        )
        trajectory["cross_loop"] = episode
        trajectories.append(trajectory)
        cross_loop_episodes.append(episode)

        counters["tasks"] += 1
        counters["cross_loop_episodes"] += 1
        counters[f"task_status_{task['status']}"] += 1
        counters["expected_attempts"] += task["attempts"]
        counters["matched_sessions"] += len(sessions)
        counters["raw_turns"] += sum(int((session.get("signals") or {}).get("turnCount") or 0) for session in sessions)
        counters["raw_native_calls"] += sum(
            int((session.get("signals") or {}).get("toolCallCount") or 0) for session in sessions
        )
        counters["raw_native_failures"] += sum(
            int((session.get("signals") or {}).get("toolFailureCount") or 0) for session in sessions
        )
        counters["successful_attempt_model_calls"] += int(((task.get("result") or {}).get("model_calls") or 0))
        counters["discarded_non_authoritative_turns"] += sum(
            int(session.get("discarded_non_authoritative_turn_count") or 0) for session in sessions
        )
        if decision_row is not None:
            counters["decisions_joined"] += 1
            if task["status"] == "done":
                counters["done_decisions_joined"] += 1
            if process_identity == "exact":
                counters["decision_process_identity_exact"] += 1
        if process_rows:
            counters["processes_joined"] += 1
            counters["process_event_rows_joined"] += len(process_rows)
        if decision_id and fills.get(decision_id):
            counters["decisions_with_fills"] += 1
        if decision_id in outcomes:
            counters["decisions_with_learning_outcome"] += 1
        if episode["hypotheses"]["trader"]["structured_thesis_status"] == "present":
            counters["episodes_with_structured_thesis"] += 1
        if episode["hypotheses"]["trader"].get("rationale"):
            counters["episodes_with_rationale"] += 1
        if episode["hypotheses"]["trader"]["evaluate_trade_plan_results"]:
            counters["episodes_with_trade_plan_evaluation"] += 1
        for edge, lineage in episode["lineage"].items():
            status = _clean_text((lineage or {}).get("status")) or "absent"
            counters[f"cross_lineage::{edge}::{status}"] += 1
        if following is not None:
            counters["tasks_with_later_task_observation"] += 1
        if len(sessions) == task["attempts"]:
            counters["tasks_with_complete_session_count"] += 1
        if attempts and all(attempt["prompt_shared_context_matches_task"] for attempt in attempts):
            counters["tasks_with_prompt_shared_context_match"] += 1
        if attempts and all(attempt["prompt_symbol_facts_matches_task"] for attempt in attempts):
            counters["tasks_with_prompt_symbol_facts_match"] += 1
        if attempts and len(prompt_observation_hashes) == 1:
            counters["tasks_with_consistent_prompt_observation"] += 1
        if _contains_nonfinite(task["payload"]):
            counters["task_payloads_with_nonfinite"] += 1

    task_ids = {task["id"] for task in tasks}
    log_task_ids = {task_id for task_id in daemon if task_id in task_ids}
    decision_source_in_window = Counter()
    if tasks:
        cycle_values = [task["cycle_dt"] for task in tasks if isinstance(task.get("cycle_dt"), datetime)]
        if cycle_values:
            low, high = min(cycle_values), max(cycle_values)
            for row in decisions:
                try:
                    ts = _parse_iso(str(row.get("cycle_ts")))
                except (TypeError, ValueError):
                    continue
                if low <= ts <= high:
                    decision_source_in_window[str(row.get("decision_source") or "unknown")] += 1

    attempt_distribution = Counter(str(row["queue"]["attempts_total"]) for row in trajectories)
    queue_outcomes = Counter(
        str(attempt.get("queue_outcome") or "missing") for row in trajectories for attempt in row["attempts"]
    )
    decision_actions = Counter(str((row.get("decision") or {}).get("action") or "missing") for row in trajectories)
    decision_reasons = Counter(
        str((row.get("decision") or {}).get("decision_reason_code") or "missing") for row in trajectories
    )
    turn_inputs = Counter(str(row.get("input_kind") or "missing") for row in turn_events)
    turn_outputs = Counter(str(row.get("output_kind") or "missing") for row in turn_events)
    turn_outcomes = Counter(str(row.get("turn_outcome") or "missing") for row in turn_events)
    domain_joins = Counter(str(row.get("join_status") or "missing") for row in domain_events)

    domain_sequences: dict[tuple[int, int, str], list[str]] = defaultdict(list)
    tpe_sequences: dict[tuple[int, int, str], list[bool | None]] = defaultdict(list)
    tpe_rejections = Counter()
    for row in domain_events:
        attempt_key = (
            int(row["task_id"]),
            int(row["queue_attempt_no"]),
            str(row["session_id"]),
        )
        tool_name = str(row.get("tool") or "missing")
        domain_sequences[attempt_key].append(tool_name)
        if tool_name == "evaluate_trade_plan":
            response = row.get("response") if isinstance(row.get("response"), dict) else {}
            valid = response.get("semantic_valid")
            tpe_sequences[attempt_key].append(valid if isinstance(valid, bool) else None)
            tpe_rejections.update(str(code) for code in response.get("rejection_codes") or [])

    domain_sequence_counts = Counter(" -> ".join(value) for value in domain_sequences.values())
    tpe_sequence_counts = Counter(
        " -> ".join("valid" if value is True else "invalid" if value is False else "unknown" for value in sequence)
        for sequence in tpe_sequences.values()
    )
    tpe_started_invalid = sum(sequence[0] is False for sequence in tpe_sequences.values())
    tpe_recovered_to_valid = sum(
        any(
            current is False and any(later is True for later in sequence[index + 1 :])
            for index, current in enumerate(sequence)
        )
        for sequence in tpe_sequences.values()
    )
    tpe_ending_valid = sum(sequence[-1] is True for sequence in tpe_sequences.values())

    native_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in native_events:
        native_by_session[str(row["session_id"])].append(row)
    native_call_counts = sorted(len(rows) for rows in native_by_session.values())
    native_sessions_with_error = sum(
        any(row.get("event_outcome") == "error" for row in rows) for rows in native_by_session.values()
    )
    first_native_calls = Counter(
        f"{rows[0].get('tool_name') or 'missing'}:{rows[0].get('event_outcome') or 'missing'}"
        for rows in native_by_session.values()
        if rows
    )
    first_native_categories = Counter(
        (
            f"{((rows[0].get('arguments') or {}).get('category') or 'missing')}:"
            f"{rows[0].get('event_outcome') or 'missing'}"
        )
        for rows in native_by_session.values()
        if rows
    )

    cross_lineage: dict[str, Counter[str]] = defaultdict(Counter)
    trader_flair_maturity = Counter()
    trader_flair_basis = Counter()
    trader_flair_eligibility = Counter()
    universe_flair_status = Counter()
    for episode in cross_loop_episodes:
        for edge, lineage in episode.get("lineage", {}).items():
            cross_lineage[str(edge)][_clean_text((lineage or {}).get("status")) or "absent"] += 1
        trader_target = episode["target"]["trader_flair"]
        trader_flair_maturity[_clean_text(trader_target.get("maturity")) or "absent"] += 1
        trader_flair_basis[_clean_text(trader_target.get("basis")) or "absent"] += 1
        trader_flair_eligibility[
            _clean_text((trader_target.get("eligibility") or {}).get("status")) or "missing_trace"
        ] += 1
        universe_target = episode["target"]["universe_selection_flair"]
        universe_flair_status[_clean_text(universe_target.get("status")) or "absent"] += 1

    action_ready = sum(
        (episode.get("training_eligibility") or {}).get("action_ex_ante", {}).get("status") == "eligible"
        for episode in cross_loop_episodes
    )
    outcome_ready = sum(
        episode["target"]["trader_flair"].get("trainable_label") is True for episode in cross_loop_episodes
    )

    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if tasks else "insufficient_data",
        "filters": {
            "task_id_min": config.task_id_min,
            "task_id_max": config.task_id_max,
            "since": _iso(config.since),
            "until": _iso(config.until),
        },
        "inputs": {
            "state_dir": str(config.state_dir),
            "sessions_dir": str(config.sessions_dir),
            "daemon_log": str(config.daemon_log),
            "decision_rows_read": len(decisions),
            "decision_rows_malformed": malformed_decisions,
            "session_scan": session_scan,
            "daemon_tasks_with_trace": len(log_task_ids),
            "universe_sources": universe_source_counts,
            "universe_selection_outcomes": selection_source_quality,
            "execution_cycles": execution_source_quality,
        },
        "coverage": {
            "tasks": counters["tasks"],
            "task_status": {
                "done": counters["task_status_done"],
                "dead": counters["task_status_dead"],
                "other": counters["tasks"] - counters["task_status_done"] - counters["task_status_dead"],
            },
            "expected_attempts": counters["expected_attempts"],
            "matched_raw_sessions": counters["matched_sessions"],
            "raw_session_match_rate": _ratio(counters["matched_sessions"], counters["expected_attempts"]),
            "tasks_with_complete_session_count": counters["tasks_with_complete_session_count"],
            "tasks_with_prompt_shared_context_match": counters["tasks_with_prompt_shared_context_match"],
            "tasks_with_prompt_symbol_facts_match": counters["tasks_with_prompt_symbol_facts_match"],
            "tasks_with_consistent_prompt_observation": counters["tasks_with_consistent_prompt_observation"],
            "decisions_joined": counters["decisions_joined"],
            "decision_join_rate": _ratio(counters["decisions_joined"], counters["tasks"]),
            "done_decisions_joined": counters["done_decisions_joined"],
            "done_decision_join_rate": _ratio(counters["done_decisions_joined"], counters["task_status_done"]),
            "decision_process_identity_exact": counters["decision_process_identity_exact"],
            "processes_joined": counters["processes_joined"],
            "process_event_rows_joined": counters["process_event_rows_joined"],
            "tasks_with_later_task_observation": counters["tasks_with_later_task_observation"],
            "decisions_with_fills": counters["decisions_with_fills"],
            "decisions_with_learning_outcome": counters["decisions_with_learning_outcome"],
            "decisions_by_source_in_task_window": dict(sorted(decision_source_in_window.items())),
            "task_payloads_with_nonfinite_replaced_on_export": counters["task_payloads_with_nonfinite"],
        },
        "grok_mechanics": {
            "raw_acp_turns": counters["raw_turns"],
            "successful_attempt_model_calls": counters["successful_attempt_model_calls"],
            "failed_attempt_turns_not_in_task_result": counters["raw_turns"]
            - counters["successful_attempt_model_calls"],
            "discarded_non_authoritative_turn_rows": counters["discarded_non_authoritative_turns"],
            "assistant_messages": sum(
                int(attempt.get("assistant_message_count") or 0) for row in trajectories for attempt in row["attempts"]
            ),
            "summary_model_sessions": dict(sorted(summary_model_counts.items())),
            "assistant_model_messages": dict(sorted(assistant_model_counts.items())),
            "native_cli_calls": counters["parsed_native_event_calls"],
            "native_cli_errors": counters["parsed_native_event_errors"],
            "signals_native_cli_calls": counters["raw_native_calls"],
            "signals_native_cli_failures": counters["raw_native_failures"],
            "native_cli_call_count_delta_events_minus_signals": counters["parsed_native_event_calls"]
            - counters["raw_native_calls"],
            "native_cli_failure_count_delta_events_minus_signals": counters["parsed_native_event_errors"]
            - counters["raw_native_failures"],
            "native_tools": dict(sorted(native_tool_counts.items(), key=lambda item: (-item[1], item[0]))),
            "native_categories": dict(sorted(native_category_counts.items(), key=lambda item: (-item[1], item[0]))),
            "domain_tools": dict(sorted(domain_tool_counts.items(), key=lambda item: (-item[1], item[0]))),
            "queue_parser_errors": dict(sorted(parser_error_counts.items(), key=lambda item: (-item[1], item[0]))),
        },
        "cross_loop": {
            "schema_version": CROSS_LOOP_SCHEMA_VERSION,
            "episodes": counters["cross_loop_episodes"],
            "primary_target": "target.trader_flair",
            "hypotheses": {
                "structured_thesis_present": counters["episodes_with_structured_thesis"],
                "rationale_present": counters["episodes_with_rationale"],
                "evaluate_trade_plan_present": counters["episodes_with_trade_plan_evaluation"],
            },
            "targets": {
                "trader_flair": {
                    "maturity": dict(sorted(trader_flair_maturity.items())),
                    "basis": dict(sorted(trader_flair_basis.items())),
                    "eligibility": dict(sorted(trader_flair_eligibility.items())),
                    "trainable_labels": outcome_ready,
                    "with_verdict": sum(
                        episode["target"]["trader_flair"].get("verdict") is not None for episode in cross_loop_episodes
                    ),
                    "with_outcome_score": sum(
                        episode["target"]["trader_flair"].get("outcome_score") is not None
                        for episode in cross_loop_episodes
                    ),
                },
                "universe_selection_flair": {
                    "primary_basis": "allocation",
                    "status": dict(sorted(universe_flair_status.items())),
                    "separate_from_trader_flair": True,
                },
                "execution": {
                    "with_immediate_fill": sum(
                        episode["target"]["execution"].get("has_execution_fill") for episode in cross_loop_episodes
                    ),
                    "with_realised_cycle": sum(
                        episode["target"]["execution"].get("has_realised_cycle") for episode in cross_loop_episodes
                    ),
                    "fee_complete": sum(
                        episode["target"]["execution"].get("fee_complete") for episode in cross_loop_episodes
                    ),
                },
            },
            "lineage": {
                edge: {status: counts.get(status, 0) for status in sorted(_LINEAGE_STATUSES)}
                for edge, counts in sorted(cross_lineage.items())
            },
            "analysis_readiness": {
                "action_ex_ante_episodes": action_ready,
                "mature_trader_flair_episodes": outcome_ready,
                "allowed_uses": [
                    "trace_coverage_audit",
                    "workflow_process_mining",
                    "universe_context_trader_flair_ablation",
                ],
                "world_model_status": "not_a_world_episode_dataset",
            },
        },
        "observed_patterns": {
            "cohort": {
                "unique_symbols": len({row["identity"]["symbol"] for row in trajectories}),
                "unique_cycles": len({row["identity"]["cycle_id"] for row in trajectories}),
                "cycle_start": min((row["identity"]["cycle_id"] for row in trajectories), default=None),
                "cycle_end": max((row["identity"]["cycle_id"] for row in trajectories), default=None),
                "task_attempt_count_distribution": dict(sorted(attempt_distribution.items())),
                "queue_attempt_outcomes": dict(sorted(queue_outcomes.items())),
            },
            "decisions": {
                "actions": dict(sorted(decision_actions.items())),
                "reason_codes": dict(sorted(decision_reasons.items(), key=lambda item: (-item[1], item[0]))),
            },
            "turns": {
                "input_kinds": dict(sorted(turn_inputs.items())),
                "output_kinds": dict(sorted(turn_outputs.items())),
                "outcomes": dict(sorted(turn_outcomes.items())),
            },
            "native_cli": {
                "sessions_with_calls": len(native_by_session),
                "sessions_with_event_error": native_sessions_with_error,
                "calls_per_session_min": min(native_call_counts) if native_call_counts else None,
                "calls_per_session_median": statistics.median(native_call_counts) if native_call_counts else None,
                "calls_per_session_max": max(native_call_counts) if native_call_counts else None,
                "first_call_tool_and_outcome": dict(sorted(first_native_calls.items())),
                "first_call_category_and_outcome": dict(sorted(first_native_categories.items())),
            },
            "domain_tools": {
                "attempts_with_tools": len(domain_sequences),
                "join_status": dict(sorted(domain_joins.items())),
                "sequence_counts": dict(
                    sorted(
                        domain_sequence_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ),
            },
            "evaluate_trade_plan": {
                "attempts": len(tpe_sequences),
                "calls": sum(len(sequence) for sequence in tpe_sequences.values()),
                "semantic_valid_calls": sum(value is True for sequence in tpe_sequences.values() for value in sequence),
                "semantic_invalid_calls": sum(
                    value is False for sequence in tpe_sequences.values() for value in sequence
                ),
                "attempts_started_invalid": tpe_started_invalid,
                "attempts_recovered_to_valid": tpe_recovered_to_valid,
                "attempts_ending_valid": tpe_ending_valid,
                "sequence_counts": dict(sorted(tpe_sequence_counts.items(), key=lambda item: (-item[1], item[0]))),
                "rejection_codes": dict(sorted(tpe_rejections.items(), key=lambda item: (-item[1], item[0]))),
            },
        },
        "known_limits": [
            "raw_grok_sessions_join_tasks_by_reconstructed_cycle_id+symbol_not_explicit_task_id",
            "prompt_symbol_block_is_a_runtime_projection_and_can_differ_from_task_payload",
            "task_result_model_calls_excludes_failed_queue_attempts",
            "ledger_tool_outcome_ok_does_not_imply_domain_semantic_valid",
            "later_task_observation_is_diagnostic_scheduler_chronology_not_a_market_transition",
            "mechanical_planned_exits_without_decision_id_need_episode_level_linkage",
            "legacy_universe_runs_without_request_snapshot_cannot_recover_selection_feedback",
            "legacy_counterfactual_notes_may_not_persist_the_exact_1d_or_4h_horizon",
            "structured_trader_thesis_is_optional_and_rationale_is_often_unstructured_text",
            "cross_channel_event_interleaving_is_not_persisted",
            "realised_trader_flair_is_withheld_until_a_fee_complete_flat_to_flat_cycle_exists",
        ],
    }
    datasets = {
        "trajectories": trajectories,
        "cross_loop_episodes": cross_loop_episodes,
        "grok_native_steps": native_events,
        "grok_turns": turn_events,
        "domain_tool_steps": domain_events,
    }
    return summary, datasets


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    _json_safe(row),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            handle.write("\n")


def write_datasets(output_dir: Path, summary: dict[str, Any], datasets: dict[str, list[dict[str, Any]]]) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in datasets.items():
        _write_jsonl(output_dir / f"{name}.jsonl", rows)
    (output_dir / "summary.json").write_text(
        json.dumps(
            _json_safe(summary),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _human_report(summary: dict[str, Any]) -> str:
    coverage = summary["coverage"]
    mechanics = summary["grok_mechanics"]
    lines = [
        f"status: {summary['status']}",
        ("tasks: {tasks} (done={done}, dead={dead}) | attempts: {matched}/{expected} sessions").format(
            tasks=coverage["tasks"],
            done=coverage["task_status"]["done"],
            dead=coverage["task_status"]["dead"],
            matched=coverage["matched_raw_sessions"],
            expected=coverage["expected_attempts"],
        ),
        (
            "decisions: {joined}/{tasks} | processes: {processes}/{tasks} | "
            "later same-symbol tasks: {later}/{tasks}"
        ).format(
            joined=coverage["decisions_joined"],
            processes=coverage["processes_joined"],
            later=coverage["tasks_with_later_task_observation"],
            tasks=coverage["tasks"],
        ),
        (
            "Grok: {turns} ACP turns, {native} native CLI calls ({failures} failures), "
            "{hidden} failed-attempt turns absent from task.result.model_calls"
        ).format(
            turns=mechanics["raw_acp_turns"],
            native=mechanics["native_cli_calls"],
            failures=mechanics["native_cli_errors"],
            hidden=mechanics["failed_attempt_turns_not_in_task_result"],
        ),
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument("--sessions-dir", type=Path, default=Path("ops/grok-home/sessions"))
    parser.add_argument("--daemon-log", type=Path, default=Path("state/daemon_console.log"))
    parser.add_argument("--task-id-min", type=int)
    parser.add_argument("--task-id-max", type=int)
    parser.add_argument("--since", help="ISO-8601 lower bound on task cycle_id")
    parser.add_argument("--until", help="ISO-8601 upper bound on task cycle_id")
    parser.add_argument(
        "--include-shared-context",
        action="store_true",
        help="include the full shared state in trajectories.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="explicitly write datasets to a new or empty directory",
    )
    parser.add_argument("--json", action="store_true", help="print the coverage report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        since = _parse_iso(args.since) if args.since else None
        until = _parse_iso(args.until) if args.until else None
    except ValueError as exc:
        parser.error(f"invalid ISO date: {exc}")
    config = SpikeConfig(
        state_dir=args.state_dir,
        sessions_dir=args.sessions_dir,
        daemon_log=args.daemon_log,
        task_id_min=args.task_id_min,
        task_id_max=args.task_id_max,
        since=since,
        until=until,
        include_shared_context=args.include_shared_context,
        output_dir=args.output_dir,
    )
    try:
        summary, datasets = reconstruct(config)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"cannot reconstruct spike: {exc}", file=sys.stderr)
        return 2
    if args.output_dir is not None:
        try:
            write_datasets(args.output_dir, summary, datasets)
        except (OSError, ValueError) as exc:
            print(f"cannot write spike output: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(
            json.dumps(
                _json_safe(summary),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        )
    else:
        print(_human_report(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
