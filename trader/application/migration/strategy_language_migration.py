"""State migration helpers for the canonical strategy language."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

INTENT_VALUE_MAP = {
    "ADD": "SCALE_IN",
    "REVERSE": "FLIP",
}

KEY_MAP = {
    "amend_exit": "exit_update",
    "amend_exit_applied": "exit_update_applied",
    "amend_exit_reason": "exit_update_reason",
    "amend_exit_warnings": "exit_update_warnings",
    "amend_exit_trace": "exit_update_trace",
}

REASON_VALUE_MAP = {
    "add_without_position": "scale_in_without_position",
    "empty_amend": "empty_update",
    "amend_exit_conflicts_with_propose_order": "strategy_exit_conflicts_with_strategy_entry",
    "exit_update_conflicts_with_propose_order": "strategy_exit_conflicts_with_strategy_entry",
}

TOKEN_TEXT_MAP = {
    "propose_order": "strategy_entry",
    "amend_exit": "exit_update",
}


def _enrich_strategy_exit_details(value: dict[str, Any]) -> None:
    reason = value.get("exit_update_reason")
    if not isinstance(reason, str) or not reason:
        return
    calls = value.get("tool_calls")
    if not isinstance(calls, list):
        return
    for call in calls:
        if not isinstance(call, dict) or call.get("tool") != "strategy_exit":
            continue
        detail = call.get("detail") if isinstance(call.get("detail"), dict) else {}
        detail.setdefault("reason", reason)
        if value.get("exit_update_applied") is not None:
            detail.setdefault("exit_update_applied", value["exit_update_applied"])
        call["detail"] = detail


def _copy_exit_update_audit_to_runtime(value: dict[str, Any]) -> None:
    decision = value.get("decision")
    runtime = value.get("runtime")
    if not isinstance(decision, dict) or not isinstance(runtime, dict):
        return
    for key in (
        "exit_update_applied",
        "exit_update_reason",
        "exit_update_trace",
        "exit_update_warnings",
    ):
        if key in decision and key not in runtime:
            runtime[key] = decision[key]
    _enrich_strategy_exit_details(runtime)


def _mapped_string(key: str | None, value: str) -> str:
    if key == "intent":
        return INTENT_VALUE_MAP.get(value, value)
    if key in {"reason", "rationale", "exit_update_reason", "amend_exit_reason"}:
        value = REASON_VALUE_MAP.get(value, value)
    for old, new in TOKEN_TEXT_MAP.items():
        value = value.replace(old, new)
    value = re.sub(r"\bADD\b", "SCALE_IN", value)
    value = re.sub(r"\bREVERSE\b", "FLIP", value)
    return value


def _entry_direction_from_args(args: dict[str, Any]) -> str | None:
    raw = args.get("direction") or args.get("side") or args.get("action")
    if raw is None:
        intent = str(args.get("intent") or "").upper()
        if intent == "OPEN_LONG":
            return "long"
        if intent == "OPEN_SHORT":
            return "short"
        return None
    side = str(raw).lower().replace("strategy.", "")
    if side in {"long", "buy"}:
        return "long"
    if side in {"short", "sell"}:
        return "short"
    return None


def _migrate_propose_order_call(call: dict[str, Any]) -> dict[str, Any]:
    args = call.get("args")
    args = dict(args) if isinstance(args, dict) else {}
    intent = INTENT_VALUE_MAP.get(str(args.get("intent") or "").upper(), str(args.get("intent") or "").upper())

    migrated = dict(call)
    if intent in {"CLOSE", "REDUCE"}:
        close_args: dict[str, Any] = {}
        if intent == "REDUCE":
            if args.get("fraction") is not None:
                close_args["qty_percent"] = float(args["fraction"]) * 100.0
            elif args.get("qty") is not None:
                close_args["qty"] = args["qty"]
            elif args.get("quantity") is not None:
                close_args["quantity"] = args["quantity"]
        migrated["tool"] = "strategy_close"
        migrated["args"] = close_args
        return migrated

    entry_args = {k: v for k, v in args.items() if k not in {"intent", "side", "action"}}
    if "exit_plan" in entry_args:
        if "exit" not in entry_args:
            entry_args["exit"] = entry_args["exit_plan"]
        del entry_args["exit_plan"]
    direction = _entry_direction_from_args(args)
    if direction is not None:
        entry_args["direction"] = direction
    migrated["tool"] = "strategy_entry"
    migrated["args"] = migrate_strategy_language_value(entry_args)
    return migrated


def _migrate_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    tool = call.get("tool")
    if tool == "propose_order":
        return _migrate_propose_order_call(call)
    migrated = dict(call)
    if tool in {"amend_exit", "exit_update"}:
        migrated["tool"] = "strategy_exit"
    if isinstance(migrated.get("args"), dict):
        migrated["args"] = migrate_strategy_language_value(migrated["args"])
    return migrated


def migrate_strategy_language_value(value: Any, *, key: str | None = None) -> Any:
    """Return an idempotently migrated JSON-compatible value."""
    if isinstance(value, dict):
        if isinstance(value.get("tool"), str):
            return _migrate_tool_call(value)
        migrated: dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            new_key = KEY_MAP.get(str(raw_key), str(raw_key))
            migrated[new_key] = migrate_strategy_language_value(raw_val, key=new_key)
        _copy_exit_update_audit_to_runtime(migrated)
        _enrich_strategy_exit_details(migrated)
        return migrated
    if isinstance(value, list):
        return [migrate_strategy_language_value(item, key=key) for item in value]
    if isinstance(value, str):
        return _mapped_string(key, value)
    return value


def migrate_jsonl_file(path: Path) -> int:
    rows = path.read_text(encoding="utf-8").splitlines()
    migrated_rows: list[str] = []
    changed = 0
    for line in rows:
        if not line.strip():
            migrated_rows.append(line)
            continue
        migrated = migrate_strategy_language_value(json.loads(line))
        encoded = json.dumps(migrated, ensure_ascii=False, separators=(",", ":"))
        if encoded != line:
            changed += 1
        migrated_rows.append(encoded)
    if changed:
        path.write_text("\n".join(migrated_rows) + ("\n" if rows else ""), encoding="utf-8")
    return changed


def migrate_json_file(path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    migrated = migrate_strategy_language_value(data)
    if migrated == data:
        return False
    path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True
