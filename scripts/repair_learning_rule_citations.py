"""Restore lost global_rule_citations from canonical decision ledgers.

Dry-run by default. Pass --apply to insert pending citation rows only.
Uses stdlib sqlite3 against an existing learnings.db; never migrates schema.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

DB_NAME = "learnings.db"
LEDGER_NAME = "decisions.jsonl"
ARCHIVE_JSONL_GLOB = "decisions-????-??.jsonl"
ARCHIVE_GZIP_GLOB = "decisions-????-??.jsonl.gz"
OUTCOME_SEMANTICS_VERSION = "3"
BUSY_TIMEOUT_MS = 5000
MAX_RULE_IDS = 3
CITATION_COLUMNS = frozenset(
    {
        "id",
        "decision_id",
        "rule_ids",
        "ts",
        "verdict",
        "reward",
        "forward_return",
        "evaluated_at",
        "outcome_semantics_version",
    }
)
IDENTITY_FIELDS = (
    "decision_id",
    "cycle_ts",
    "symbol",
    "applied_learning_ids",
    "decision_source",
    "model_called",
    "llm_error",
    "dry_run",
)
FIELD_NESTING = {
    "decision_id": ("decision", "process"),
    "cycle_ts": ("decision",),
    "symbol": ("decision",),
    "applied_learning_ids": ("decision",),
    "decision_source": ("decision",),
    "model_called": ("decision",),
    "llm_error": ("decision",),
    "dry_run": ("runtime", "decision"),
}

PidProbe = Callable[[int], bool]
Hook = Callable[[], None]


class RepairRefused(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def probe_pid(pid: int) -> bool:
    """Return True if pid is live. Probe failures must not look like a dead pid."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise RepairRefused("daemon_not_stopped", f"pid {pid} probe failed") from exc
    return True


def line_sha256(line: str) -> str:
    return hashlib.sha256(line.rstrip("\r\n").encode("utf-8")).hexdigest()


def parse_aware(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def sqlite_uri(path: Path, *, mode: str) -> str:
    encoded = quote(str(path.resolve()), safe="/")
    if mode == "ro":
        return f"file:{encoded}?mode=ro"
    if mode == "rw":
        return f"file:{encoded}?mode=rw"
    raise RepairRefused("invalid_db_mode", f"unsupported sqlite mode {mode!r}")


def connect_db(path: Path, *, mode: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        sqlite_uri(path, mode=mode),
        uri=True,
        isolation_level=None,
        timeout=BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)}")
    return conn


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def assert_output_allowed(output: Path, state_dir: Path, db_path: Path) -> None:
    out = output.resolve()
    state = state_dir.resolve()
    if out == state or out == db_path.resolve() or _is_inside(out, state):
        raise RepairRefused("output_forbidden", f"output path is not allowed: {out}")


def _read_pid_value(raw: object, *, source: str) -> int:
    if raw in (None, ""):
        raise RepairRefused("daemon_not_stopped", f"{source}: malformed pid")
    if isinstance(raw, bool) or isinstance(raw, float):
        raise RepairRefused("daemon_not_stopped", f"{source}: malformed pid")
    if isinstance(raw, int):
        pid = raw
    else:
        text = str(raw).strip()
        if not text:
            raise RepairRefused("daemon_not_stopped", f"{source}: malformed pid")
        if not text.isdigit():
            raise RepairRefused("daemon_not_stopped", f"{source}: malformed pid")
        pid = int(text)
    if pid <= 0:
        raise RepairRefused("daemon_not_stopped", f"{source}: malformed pid")
    return pid


def _pid_file_value(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RepairRefused("daemon_not_stopped", f"{path}: unreadable") from exc
    return _read_pid_value(raw.strip(), source=str(path))


def _status_pid(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except OSError as exc:
        raise RepairRefused("daemon_not_stopped", f"{path}: unreadable") from exc
    except json.JSONDecodeError as exc:
        raise RepairRefused("daemon_not_stopped", f"{path}: malformed") from exc
    if not isinstance(payload, dict):
        raise RepairRefused("daemon_not_stopped", f"{path}: malformed")
    return _read_pid_value(payload.get("pid"), source=str(path))


def assert_daemon_stopped(state_dir: Path, *, pid_probe: PidProbe = probe_pid) -> None:
    referenced: list[tuple[str, int]] = []
    status_path = state_dir / "daemon_status.json"
    pid_path = state_dir / "daemon.pid"
    if status_path.exists():
        referenced.append(("daemon_status.json", _status_pid(status_path)))
    if pid_path.exists():
        referenced.append(("daemon.pid", _pid_file_value(pid_path)))
    for source, pid in referenced:
        try:
            live = pid_probe(pid)
        except RepairRefused:
            raise
        except Exception as exc:
            raise RepairRefused("daemon_not_stopped", f"{source}: pid {pid} probe failed") from exc
        if live:
            raise RepairRefused("daemon_not_stopped", f"{source}: pid {pid} is live")


def _safe_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise RepairRefused("schema_invalid", f"unsafe identifier {name!r}")
    return name


def _has_unique_decision_id(conn: sqlite3.Connection) -> bool:
    indexes = conn.execute("PRAGMA index_list(global_rule_citations)").fetchall()
    for index in indexes:
        unique = int(index[2])
        name = str(index[1])
        partial = int(index[4]) if len(index) > 4 else 0
        if not unique or partial:
            continue
        columns = [
            str(info[2])
            for info in conn.execute(f"PRAGMA index_info({_safe_ident(name)})").fetchall()
        ]
        if columns == ["decision_id"]:
            return True
    return False


def validate_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {"global_rule_citations", "global_rules", "learnings_metadata"}
    if not required <= tables:
        raise RepairRefused("schema_invalid", "missing required tables")
    citation_cols = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(global_rule_citations)")
    }
    if citation_cols != CITATION_COLUMNS:
        raise RepairRefused("schema_invalid", "unexpected global_rule_citations columns")
    rule_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(global_rules)")}
    if "rule_id" not in rule_cols:
        raise RepairRefused("schema_invalid", "global_rules.rule_id missing")
    if not _has_unique_decision_id(conn):
        raise RepairRefused("schema_invalid", "UNIQUE(decision_id) missing")
    meta = conn.execute(
        "SELECT value FROM learnings_metadata WHERE key='outcome_semantics_version'"
    ).fetchone()
    if meta is None or str(meta[0]) != OUTCOME_SEMANTICS_VERSION:
        raise RepairRefused("schema_invalid", "outcome_semantics_version must be 3")


def source_paths(state_dir: Path) -> list[Path]:
    archive = state_dir / "archive"
    found: list[Path] = []
    if archive.is_dir():
        found.extend(archive.glob(ARCHIVE_JSONL_GLOB))
        found.extend(archive.glob(ARCHIVE_GZIP_GLOB))
        found = sorted({path.resolve() for path in found}, key=lambda path: (path.name, str(path)))
    live = state_dir / LEDGER_NAME
    if live.is_file():
        found.append(live.resolve())
    return found


def _open_source(path: Path):
    if path.name.endswith(".jsonl.gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("rt", encoding="utf-8")


def iter_source_lines(state_dir: Path) -> list[tuple[Path, int, str]]:
    rows: list[tuple[Path, int, str]] = []
    for path in source_paths(state_dir):
        try:
            with _open_source(path) as handle:
                for lineno, line in enumerate(handle, start=1):
                    rows.append((path, lineno, line))
        except OSError as exc:
            raise RepairRefused("unreadable_source", f"{path}: {exc}") from exc
    return rows


def _source_ref(path: Path, lineno: int, line: str) -> dict[str, Any]:
    return {"path": str(path.resolve()), "line": lineno, "sha256": line_sha256(line)}


def _maps_for(row: dict[str, Any]) -> list[dict[str, Any]]:
    maps = [row]
    for key in ("decision", "runtime", "process"):
        nested = row.get(key)
        if isinstance(nested, dict):
            maps.append(nested)
    return maps


def nested_fields_mismatch(row: dict[str, Any]) -> bool:
    maps = _maps_for(row)
    for field in IDENTITY_FIELDS:
        values = [mapping[field] for mapping in maps if field in mapping]
        if len(values) >= 2 and any(value != values[0] for value in values[1:]):
            return True
    return False


def _field(row: dict[str, Any], field: str) -> object:
    if field in row:
        return row[field]
    for nest in FIELD_NESTING.get(field, ()):
        nested = row.get(nest)
        if isinstance(nested, dict) and field in nested:
            return nested[field]
    return None


def _llm_error_present(value: object) -> bool:
    return value not in (None, "", False)


def _parse_rule_ids(raw: object, *, present: bool) -> tuple[str, list[str]]:
    if not present or raw is None:
        return "empty", []
    if not isinstance(raw, list):
        return "malformed", []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return "malformed", []
        rule_id = item.strip()
        if not rule_id:
            return "malformed", []
        if rule_id in seen:
            return "malformed", []
        seen.add(rule_id)
        cleaned.append(rule_id)
    if not cleaned:
        return "empty", []
    if len(cleaned) > MAX_RULE_IDS:
        return "malformed", []
    return "ok", cleaned


def _normalize_rule_ids(raw: object) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    cleaned: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            return None
        cleaned.append(text)
    return cleaned


def classify_line(
    payload: object,
    *,
    since: datetime,
    path: Path,
    lineno: int,
    line: str,
) -> dict[str, Any]:
    source = _source_ref(path, lineno, line)
    if not isinstance(payload, dict):
        return {"kind": "skipped", "reason": "skipped_invalid_row", "sources": [source]}
    cycle_raw = _field(payload, "cycle_ts")
    cycle_dt = parse_aware(cycle_raw) if cycle_raw not in (None, "") else None
    if cycle_dt is not None and cycle_dt < since:
        return {
            "kind": "skipped",
            "reason": "skipped_before_since",
            "decision_id": str(_field(payload, "decision_id") or "") or None,
            "cycle_ts": str(cycle_raw) if cycle_raw not in (None, "") else None,
            "sources": [source],
        }
    decision_source = _field(payload, "decision_source")
    if decision_source != "llm":
        return {
            "kind": "skipped",
            "reason": "skipped_not_llm",
            "decision_id": str(_field(payload, "decision_id") or "") or None,
            "sources": [source],
        }
    if _field(payload, "model_called") is not True:
        return {
            "kind": "skipped",
            "reason": "skipped_model_not_called",
            "decision_id": str(_field(payload, "decision_id") or "") or None,
            "sources": [source],
        }
    if _llm_error_present(_field(payload, "llm_error")):
        return {
            "kind": "skipped",
            "reason": "skipped_llm_error",
            "decision_id": str(_field(payload, "decision_id") or "") or None,
            "sources": [source],
        }
    dry_values = [
        mapping["dry_run"]
        for mapping in _maps_for(payload)
        if "dry_run" in mapping
    ]
    if any(value is True for value in dry_values):
        return {
            "kind": "skipped",
            "reason": "skipped_dry_run",
            "decision_id": str(_field(payload, "decision_id") or "") or None,
            "sources": [source],
        }
    mismatch = nested_fields_mismatch(payload)
    decision_id = _field(payload, "decision_id")
    symbol = _field(payload, "symbol")
    ids_present = "applied_learning_ids" in payload or (
        isinstance(payload.get("decision"), dict) and "applied_learning_ids" in payload["decision"]
    )
    status, rule_ids = _parse_rule_ids(_field(payload, "applied_learning_ids"), present=ids_present)
    base = {
        "decision_id": str(decision_id).strip() if isinstance(decision_id, str) else None,
        "cycle_ts": None if cycle_raw in (None, "") else str(cycle_raw),
        "symbol": str(symbol).strip() if isinstance(symbol, str) else None,
        "rule_ids": rule_ids,
        "sources": [source],
    }
    if mismatch:
        return {**base, "kind": "conflict", "reason": "conflict_nested_field_mismatch"}
    if cycle_dt is None or cycle_raw in (None, ""):
        return {**base, "kind": "conflict", "reason": "conflict_malformed_cycle_ts"}
    if not isinstance(decision_id, str) or not decision_id.strip():
        return {**base, "kind": "conflict", "reason": "conflict_malformed_decision_id"}
    if not isinstance(symbol, str) or not symbol.strip():
        return {**base, "kind": "conflict", "reason": "conflict_missing_symbol"}
    if status == "empty":
        return {**base, "kind": "skipped", "reason": "skipped_empty_citations"}
    if status == "malformed":
        return {**base, "kind": "conflict", "reason": "conflict_malformed_rule_ids"}
    return {
        **base,
        "kind": "candidate",
        "reason": "candidate",
        "decision_id": decision_id.strip(),
        "cycle_ts": str(cycle_raw),
        "cycle_dt": cycle_dt,
        "symbol": symbol.strip(),
        "rule_ids": rule_ids,
    }


def _merge_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in items:
        for source in item["sources"]:
            key = (source["path"], int(source["line"]), source["sha256"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(source)
    return merged


def _same_membership(left: list[str], right: list[str]) -> bool:
    return tuple(left) == tuple(right)


def _load_known_rules(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT rule_id FROM global_rules")}


def _load_citations(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT decision_id, rule_ids, ts, verdict, reward, forward_return, "
        "evaluated_at, outcome_semantics_version FROM global_rule_citations"
    ).fetchall()
    return {str(row["decision_id"]): row for row in rows}


def _citation_rule_ids(row: sqlite3.Row) -> list[str] | None:
    try:
        parsed = json.loads(row["rule_ids"])
    except (TypeError, json.JSONDecodeError):
        return None
    return _normalize_rule_ids(parsed)


def _chronology_conflicts(
    citations: dict[str, sqlite3.Row],
    insertions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    earliest: dict[str, datetime] = {}
    for item in insertions:
        for rule_id in item["rule_ids"]:
            current = earliest.get(rule_id)
            if current is None or item["cycle_dt"] < current:
                earliest[rule_id] = item["cycle_dt"]
    affected: set[str] = set()
    for row in citations.values():
        if row["evaluated_at"] is None or row["reward"] is None:
            continue
        ts = parse_aware(row["ts"])
        rule_ids = _citation_rule_ids(row)
        if ts is None or rule_ids is None:
            raise RepairRefused("schema_invalid", "malformed existing citation")
        for rule_id in rule_ids:
            start = earliest.get(rule_id)
            if start is not None and ts >= start:
                affected.add(rule_id)
    conflicts: list[dict[str, Any]] = []
    for item in insertions:
        hit = [rule_id for rule_id in item["rule_ids"] if rule_id in affected]
        if hit:
            conflicts.append(
                {
                    "kind": "conflict",
                    "reason": "conflict_chronology",
                    "decision_id": item["decision_id"],
                    "cycle_ts": item["cycle_ts"],
                    "rule_ids": item["rule_ids"],
                    "affected_rule_ids": hit,
                    "sources": item["sources"],
                }
            )
    return conflicts


def _fingerprint(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        sources = sorted(
            (source["path"], int(source["line"]), source["sha256"])
            for source in item.get("sources", [])
        )
        out.append(
            {
                "decision_id": item.get("decision_id"),
                "cycle_ts": item.get("cycle_ts"),
                "rule_ids": list(item.get("rule_ids") or []),
                "sources": sources,
            }
        )
    return out


def _canonical_candidates(plan: dict[str, Any]) -> list[dict[str, Any]]:
    items = list(plan.get("proposal") or []) + list(plan.get("already_present") or [])
    items.sort(
        key=lambda item: (
            str(item.get("decision_id") or ""),
            str(item.get("cycle_ts") or ""),
        )
    )
    return items


def build_plan(conn: sqlite3.Connection, *, state_dir: Path, since: datetime) -> dict[str, Any]:
    skipped: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path, lineno, line in iter_source_lines(state_dir):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            skipped.append(
                {
                    "kind": "skipped",
                    "reason": "skipped_invalid_json",
                    "sources": [_source_ref(path, lineno, line)],
                }
            )
            continue
        classified = classify_line(
            payload, since=since, path=path, lineno=lineno, line=line
        )
        kind = classified["kind"]
        if kind == "skipped":
            skipped.append(classified)
            continue
        if kind == "conflict":
            conflicts.append(classified)
            continue
        grouped.setdefault(str(classified["decision_id"]), []).append(classified)

    known_rules = _load_known_rules(conn)
    existing = _load_citations(conn)
    proposal: list[dict[str, Any]] = []
    already_present: list[dict[str, Any]] = []
    for decision_id, items in grouped.items():
        keys = {(item["cycle_dt"], tuple(item["rule_ids"])) for item in items}
        merged_sources = _merge_sources(items)
        first = items[0]
        if len(keys) > 1:
            conflicts.append(
                {
                    "kind": "conflict",
                    "reason": "conflict_divergent_duplicate",
                    "decision_id": decision_id,
                    "cycle_ts": first["cycle_ts"],
                    "rule_ids": first["rule_ids"],
                    "sources": merged_sources,
                }
            )
            continue
        record = {
            "decision_id": decision_id,
            "cycle_ts": first["cycle_ts"],
            "cycle_dt": first["cycle_dt"],
            "symbol": first["symbol"],
            "rule_ids": list(first["rule_ids"]),
            "sources": merged_sources,
        }
        unknown = [rule_id for rule_id in record["rule_ids"] if rule_id not in known_rules]
        if unknown:
            conflicts.append(
                {
                    **record,
                    "kind": "conflict",
                    "reason": "conflict_unknown_rule",
                    "unknown_rule_ids": unknown,
                }
            )
            continue
        current = existing.get(decision_id)
        if current is not None:
            current_ids = _citation_rule_ids(current)
            current_ts = parse_aware(current["ts"])
            if (
                current_ids is not None
                and current_ts is not None
                and current_ts == record["cycle_dt"]
                and _same_membership(current_ids, record["rule_ids"])
            ):
                already_present.append({**record, "reason": "already_present"})
            else:
                conflicts.append(
                    {
                        **record,
                        "kind": "conflict",
                        "reason": "conflict_existing_mismatch",
                    }
                )
            continue
        proposal.append(record)

    chronology = _chronology_conflicts(existing, proposal)
    if chronology:
        affected_ids = {item["decision_id"] for item in chronology}
        proposal = [item for item in proposal if item["decision_id"] not in affected_ids]
        conflicts.extend(chronology)

    proposal.sort(key=lambda item: (item["cycle_dt"], item["decision_id"]))
    already_present.sort(key=lambda item: (item["cycle_dt"], item["decision_id"]))
    return {
        "proposal": proposal,
        "already_present": already_present,
        "skipped": skipped,
        "conflicts": conflicts,
    }


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "decision_id": item.get("decision_id"),
        "cycle_ts": item.get("cycle_ts"),
        "symbol": item.get("symbol"),
        "rule_ids": list(item.get("rule_ids") or []),
        "sources": item.get("sources") or [],
    }
    if item.get("reason"):
        out["reason"] = item["reason"]
    if item.get("unknown_rule_ids"):
        out["unknown_rule_ids"] = item["unknown_rule_ids"]
    if item.get("affected_rule_ids"):
        out["affected_rule_ids"] = item["affected_rule_ids"]
    return out


def make_report(
    *,
    status: str,
    apply: bool,
    since: datetime,
    state_dir: Path,
    db_path: Path,
    output: Path,
    plan: dict[str, Any],
    inserted: list[dict[str, Any]] | None = None,
    refused_reason: str | None = None,
) -> dict[str, Any]:
    proposal = [_public_item(item) for item in plan.get("proposal", [])]
    already = [_public_item(item) for item in plan.get("already_present", [])]
    skipped = []
    for item in plan.get("skipped", []):
        row = {
            "reason": item.get("reason"),
            "decision_id": item.get("decision_id"),
            "cycle_ts": item.get("cycle_ts"),
            "sources": item.get("sources") or [],
        }
        skipped.append(row)
    conflicts = [_public_item(item) for item in plan.get("conflicts", [])]
    inserted_items = [_public_item(item) for item in (inserted or [])]
    return {
        "status": status,
        "apply": apply,
        "since": since.isoformat(),
        "state_dir": str(state_dir.resolve()),
        "db_path": str(db_path.resolve()),
        "output": str(output.resolve()),
        "summary": {
            "changed": len(inserted_items) if status == "applied" else 0,
            "planned_insertions": len(proposal),
            "already_present": len(already),
            "skipped": len(skipped),
            "conflicts": len(conflicts),
            "inserted": len(inserted_items),
        },
        "proposal": proposal,
        "plan": deepcopy(proposal),
        "already_present": already,
        "skipped": skipped,
        "conflicts": conflicts,
        "inserted": inserted_items,
        "refused_reason": refused_reason,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def empty_plan() -> dict[str, Any]:
    return {"proposal": [], "already_present": [], "skipped": [], "conflicts": []}


def _insert_rows(
    conn: sqlite3.Connection,
    proposal: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    inserted: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for item in proposal:
        conn.execute(
            "INSERT INTO global_rule_citations ("
            "decision_id, rule_ids, ts, verdict, reward, forward_return, "
            "evaluated_at, outcome_semantics_version"
            ") VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL) "
            "ON CONFLICT(decision_id) DO NOTHING",
            (item["decision_id"], json.dumps(item["rule_ids"]), item["cycle_ts"]),
        )
        changed = int(conn.execute("SELECT changes()").fetchone()[0])
        if changed == 1:
            inserted.append(item)
            continue
        row = conn.execute(
            "SELECT rule_ids, ts FROM global_rule_citations WHERE decision_id=?",
            (item["decision_id"],),
        ).fetchone()
        current_ids = _citation_rule_ids(row) if row is not None else None
        current_ts = parse_aware(row["ts"]) if row is not None else None
        if (
            row is not None
            and current_ids is not None
            and current_ts is not None
            and current_ts == item["cycle_dt"]
            and _same_membership(current_ids, item["rule_ids"])
        ):
            already.append({**item, "reason": "already_present"})
        else:
            conflicts.append({**item, "kind": "conflict", "reason": "conflict_existing_mismatch"})
    return inserted, already, conflicts


def _sqlite_refuse_reason(exc: sqlite3.Error) -> str:
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return "busy"
    return "database_error"


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def apply_plan(
    *,
    state_dir: Path,
    db_path: Path,
    since: datetime,
    frozen: dict[str, Any],
    pid_probe: PidProbe,
    before_write_transaction: Hook | None,
) -> dict[str, Any]:
    assert_daemon_stopped(state_dir, pid_probe=pid_probe)
    if before_write_transaction is not None:
        before_write_transaction()
    try:
        conn = connect_db(db_path, mode="rw")
    except sqlite3.Error as exc:
        raise RepairRefused(_sqlite_refuse_reason(exc), str(exc)) from exc
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            assert_daemon_stopped(state_dir, pid_probe=pid_probe)
            validate_schema(conn)
            rebuilt = build_plan(conn, state_dir=state_dir, since=since)
            if rebuilt["conflicts"]:
                conn.execute("ROLLBACK")
                rebuilt["refused_reason"] = "conflicts"
                return rebuilt
            if _fingerprint(_canonical_candidates(rebuilt)) != _fingerprint(
                _canonical_candidates(frozen)
            ):
                conn.execute("ROLLBACK")
                rebuilt["refused_reason"] = "source_changed"
                rebuilt["conflicts"] = list(rebuilt["conflicts"])
                return rebuilt
            inserted, concurrent_present, insert_conflicts = _insert_rows(conn, rebuilt["proposal"])
            if insert_conflicts:
                conn.execute("ROLLBACK")
                rebuilt["conflicts"] = insert_conflicts
                rebuilt["refused_reason"] = "conflicts"
                rebuilt["proposal"] = frozen["proposal"]
                return rebuilt
            conn.execute("COMMIT")
            rebuilt["inserted"] = inserted
            rebuilt["already_present"] = list(rebuilt["already_present"]) + concurrent_present
            rebuilt["proposal"] = frozen["proposal"]
            return rebuilt
        except RepairRefused:
            _rollback_quietly(conn)
            raise
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise RepairRefused(_sqlite_refuse_reason(exc), str(exc)) from exc
        except Exception:
            _rollback_quietly(conn)
            raise
    finally:
        conn.close()


def run_repair(
    *,
    state_dir: Path,
    since: datetime,
    output: Path,
    apply: bool,
    pid_probe: PidProbe = probe_pid,
    before_write_transaction: Hook | None = None,
) -> dict[str, Any]:
    state_dir = state_dir.resolve()
    output = Path(output)
    db_path = state_dir / DB_NAME
    assert_output_allowed(output, state_dir, db_path)
    plan = empty_plan()
    try:
        if since.tzinfo is None:
            raise RepairRefused("invalid_since", "--since must be timezone-aware")
        since_utc = since.astimezone(timezone.utc)
        if not db_path.is_file():
            raise RepairRefused("database_missing", f"missing {db_path}")
        conn = connect_db(db_path, mode="ro")
        try:
            validate_schema(conn)
            plan = build_plan(conn, state_dir=state_dir, since=since_utc)
        finally:
            conn.close()
        if not apply:
            report = make_report(
                status="dry_run",
                apply=False,
                since=since_utc,
                state_dir=state_dir,
                db_path=db_path,
                output=output,
                plan=plan,
            )
            write_report(output, report)
            return report
        if plan["conflicts"]:
            report = make_report(
                status="refused",
                apply=True,
                since=since_utc,
                state_dir=state_dir,
                db_path=db_path,
                output=output,
                plan=plan,
                refused_reason="conflicts",
            )
            write_report(output, report)
            return report
        applied = apply_plan(
            state_dir=state_dir,
            db_path=db_path,
            since=since_utc,
            frozen=plan,
            pid_probe=pid_probe,
            before_write_transaction=before_write_transaction,
        )
        if applied.get("refused_reason"):
            report = make_report(
                status="refused",
                apply=True,
                since=since_utc,
                state_dir=state_dir,
                db_path=db_path,
                output=output,
                plan=applied if applied.get("refused_reason") == "conflicts" else plan,
                refused_reason=str(applied["refused_reason"]),
            )
            if applied.get("refused_reason") == "source_changed":
                report["refused_reason"] = "source_changed"
            write_report(output, report)
            return report
        report = make_report(
            status="applied",
            apply=True,
            since=since_utc,
            state_dir=state_dir,
            db_path=db_path,
            output=output,
            plan={
                "proposal": applied.get("proposal", plan["proposal"]),
                "already_present": applied.get("already_present", plan["already_present"]),
                "skipped": plan["skipped"],
                "conflicts": applied.get("conflicts", []),
            },
            inserted=applied.get("inserted") or [],
        )
        write_report(output, report)
        return report
    except RepairRefused as exc:
        report = make_report(
            status="refused",
            apply=apply,
            since=since if since.tzinfo else since.replace(tzinfo=timezone.utc),
            state_dir=state_dir,
            db_path=db_path,
            output=output,
            plan=plan,
            refused_reason=exc.reason,
        )
        write_report(output, report)
        return report
    except sqlite3.Error as exc:
        report = make_report(
            status="refused",
            apply=apply,
            since=since if since.tzinfo else since.replace(tzinfo=timezone.utc),
            state_dir=state_dir,
            db_path=db_path,
            output=output,
            plan=plan,
            refused_reason=_sqlite_refuse_reason(exc),
        )
        write_report(output, report)
        return report


def _print_summary(report: dict[str, Any]) -> None:
    summary = report["summary"]
    parts = [
        f"status={report['status']}",
        f"changed={summary['changed']}",
        f"planned_insertions={summary['planned_insertions']}",
        f"already_present={summary['already_present']}",
        f"skipped={summary['skipped']}",
        f"conflicts={summary['conflicts']}",
    ]
    if report.get("apply"):
        parts.append(f"inserted={summary['inserted']}")
    print(" ".join(parts))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--since", required=True, help="Inclusive timezone-aware ISO-8601 UTC timestamp")
    parser.add_argument("--output", required=True, help="Audit JSON path outside --state-dir")
    parser.add_argument("--apply", action="store_true", help="Insert planned citations (opt-in)")
    args = parser.parse_args(argv)
    since = parse_aware(args.since)
    if since is None:
        parser.error("--since must be a timezone-aware ISO-8601 UTC timestamp")
    try:
        report = run_repair(
            state_dir=Path(args.state_dir),
            since=since,
            output=Path(args.output),
            apply=bool(args.apply),
        )
    except RepairRefused as exc:
        print(f"refused: {exc.reason}", file=sys.stderr)
        return 2
    _print_summary(report)
    return 0 if report["status"] != "refused" else 2


if __name__ == "__main__":
    raise SystemExit(main())
