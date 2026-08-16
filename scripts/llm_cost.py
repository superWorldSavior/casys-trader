#!/usr/bin/env python3
"""Extraction incrémentale du usage LLM — avant la purge hebdo des sqlite.

Diagnostic (2026-08-16) :
- ``ops/codex-home/logs_*.sqlite`` : table ``logs`` SANS colonnes tokens
  (id/ts/level/target/feedback_log_body/…). Purge sèche chaque dimanche 04:00.
- ``ops/codex-home/state_*.sqlite`` : ``threads.tokens_used`` existe (total
  de vie du thread, pas input/output/cached) — non purgé par le job.
- Sessions ``~/.acpx/sessions/*.json`` : ``request_token_usage`` souvent
  peuplé sur Codex (input/output/cache/thought) ; vide sur le pont Grok
  actuel. Archivées puis retirées si > 7 jours.
- Homes Grok du daemon (``ops/grok-home*``) : ``updates.jsonl`` porte
  input/output/cached/reasoning — non purgés par le job.

Schéma JSONL (une ligne par appel/requête/session/proxy), append dans
``state/archive/llm_usage/YYYY-MM.jsonl`` ::

    {
      "schema_version": 1,
      "id": "acpx_request:<session_stem>:<request_id>",
      "ts": "2026-08-10T05:08:39.307Z",
      "date": "2026-08-10",
      "source": "acpx_request|grok_session|codex_thread|codex_logs|acpx_call_proxy",
      "grain": "request|session|thread|call",
      "provider": "acpx|grok|openai|universe|…",
      "model": "gpt-5.6-luna"|null,
      "session": "8710:0"|null,
      "input_tokens": 31739, "output_tokens": 244,
      "cached_tokens": 8960, "reasoning_tokens": 131,
      "total_tokens": 40943,
      "dur_s": 5.4, "outcome": "ok", "n_calls": 1,
      "proxy": false,
      "chars_prompt": null, "chars_response": null
    }

Idempotence : ``id`` unique ; une ré-exécution n'ajoute pas de doublon.
Le coût $ exact est impossible aujourd'hui : pas de prix unitaire fiable,
et les tokens par appel ne sont pas toujours présents (Grok via acpx
laisse ``request_token_usage`` vide). ``--price-per-mtok`` reste un
convertisseur optionnel côté ``llm_usage.py cost``.

Usage::

    python -m scripts.llm_cost
    python -m scripts.llm_cost --state-dir PATH --acpx-sessions PATH
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = REPO_ROOT / "state"
DEFAULT_CODEX_HOME = REPO_ROOT / "ops" / "codex-home"
DEFAULT_GROK_HOMES = (
    REPO_ROOT / "ops" / "grok-home",
    REPO_ROOT / "ops" / "grok-home-medium",
)
DEFAULT_ACPX_SESSIONS = Path.home() / ".acpx" / "sessions"
DEFAULT_CONSOLE_LOG = DEFAULT_STATE_DIR / "daemon_console.log"

_ACPX_CALL = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}).*\[acpx_call\].*?"
    r"session=(?P<session>\S+).*?"
    r"provider=(?P<provider>[A-Za-z0-9_-]+).*?"
    r"dur_s=(?P<dur>[0-9.]+).*?outcome=(?P<outcome>[a-z_]+)"
)
_TOKEN_KEY_RE = re.compile(
    r"(input_tokens|output_tokens|cached_tokens|prompt_tokens|"
    r"completion_tokens|tokens_used|total_tokens|cache_read_input_tokens)",
    re.I,
)


@dataclass(frozen=True)
class UsageRow:
    id: str
    ts: str
    date: str
    source: str
    grain: str
    provider: str
    model: str | None
    session: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    dur_s: float | None
    outcome: str | None
    n_calls: int
    proxy: bool
    chars_prompt: int | None = None
    chars_response: int | None = None
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def usage_output_dir(state_dir: Path) -> Path:
    return state_dir / "archive" / "llm_usage"


def load_existing_ids(output_dir: Path) -> set[str]:
    ids: set[str] = set()
    if not output_dir.is_dir():
        return ids
    for path in sorted(output_dir.glob("*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ident = row.get("id")
                if ident:
                    ids.add(str(ident))
    return ids


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_of(ts: str) -> str:
    text = (ts or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return "unknown"


def _iso_from_unix(raw: object) -> str:
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()
    if seconds > 10_000_000_000:
        seconds //= 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _connect_sqlite(path: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            return sqlite3.connect(str(path))
        except sqlite3.Error:
            return None


def inspect_sqlite(path: Path) -> dict[str, object]:
    """Schéma réel + présence de colonnes token-like (lecture seule)."""

    report: dict[str, object] = {"path": str(path), "ok": False, "tables": {}}
    conn = _connect_sqlite(path)
    if conn is None:
        report["error"] = "unreadable"
        return report
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_info: dict[str, object] = {}
        for (name,) in tables:
            cols = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")]
            token_cols = [col for col in cols if "token" in col.lower()]
            table_info[str(name)] = {"columns": cols, "token_columns": token_cols}
        report["tables"] = table_info
        report["ok"] = True
        report["has_token_columns"] = any(
            info["token_columns"] for info in table_info.values()  # type: ignore[index]
        )
    except sqlite3.Error as exc:
        report["error"] = str(exc)
    finally:
        conn.close()
    return report


def _iter_sqlite_homes(codex_home: Path) -> list[Path]:
    if not codex_home.is_dir():
        return []
    return sorted(codex_home.glob("logs_*.sqlite")) + sorted(codex_home.glob("state_*.sqlite"))


def extract_sqlite_rows(codex_home: Path) -> Iterator[UsageRow]:
    for db_path in _iter_sqlite_homes(codex_home):
        conn = _connect_sqlite(db_path)
        if conn is None:
            continue
        try:
            tables = {
                str(name): {str(col[1]) for col in conn.execute(f"PRAGMA table_info({name})")}
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "logs" in tables:
                yield from _rows_from_logs_table(conn, db_path)
            if "threads" in tables and "tokens_used" in tables["threads"]:
                yield from _rows_from_threads_table(conn)
        except sqlite3.Error:
            continue
        finally:
            conn.close()


def _rows_from_logs_table(conn: sqlite3.Connection, db_path: Path) -> Iterator[UsageRow]:
    cols = [str(row[1]) for row in conn.execute("PRAGMA table_info(logs)")]
    colset = {name.lower() for name in cols}
    token_cols = [name for name in cols if "token" in name.lower()]
    if token_cols:
        select = ", ".join(["id", "ts", *token_cols] if "id" in colset and "ts" in colset else token_cols)
        try:
            cursor = conn.execute(f"SELECT {select} FROM logs")
        except sqlite3.Error:
            return
        for raw in cursor:
            mapping = dict(zip(select.split(", "), raw))
            ident = mapping.get("id")
            ts = _iso_from_unix(mapping.get("ts"))
            yield UsageRow(
                id=f"codex_logs:{db_path.name}:{ident}",
                ts=ts,
                date=_date_of(ts),
                source="codex_logs",
                grain="request",
                provider="acpx",
                model=None,
                session=None,
                input_tokens=_as_int(mapping.get("input_tokens") or mapping.get("prompt_tokens")),
                output_tokens=_as_int(mapping.get("output_tokens") or mapping.get("completion_tokens")),
                cached_tokens=_as_int(mapping.get("cached_tokens") or mapping.get("cache_read_input_tokens")),
                reasoning_tokens=_as_int(mapping.get("reasoning_tokens") or mapping.get("thought_tokens")),
                total_tokens=_as_int(mapping.get("total_tokens") or mapping.get("tokens_used")),
                dur_s=None,
                outcome=None,
                n_calls=1,
                proxy=False,
            )
        return
    if "feedback_log_body" not in colset:
        return
    try:
        cursor = conn.execute("SELECT id, ts, feedback_log_body FROM logs")
    except sqlite3.Error:
        return
    for ident, ts_raw, body in cursor:
        parsed = _tokens_from_log_body(body)
        if parsed is None:
            continue
        ts = _iso_from_unix(ts_raw)
        yield UsageRow(
            id=f"codex_logs:{db_path.name}:{ident}",
            ts=ts,
            date=_date_of(ts),
            source="codex_logs",
            grain="request",
            provider="acpx",
            model=parsed.get("model"),
            session=None,
            input_tokens=parsed.get("input_tokens"),
            output_tokens=parsed.get("output_tokens"),
            cached_tokens=parsed.get("cached_tokens"),
            reasoning_tokens=parsed.get("reasoning_tokens"),
            total_tokens=parsed.get("total_tokens"),
            dur_s=None,
            outcome=None,
            n_calls=1,
            proxy=False,
        )


def _tokens_from_log_body(body: object) -> dict | None:
    if not isinstance(body, str) or not _TOKEN_KEY_RE.search(body):
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return {
            "model": payload.get("model"),
            "input_tokens": _as_int(
                payload.get("input_tokens") or payload.get("prompt_tokens")
            ),
            "output_tokens": _as_int(
                payload.get("output_tokens") or payload.get("completion_tokens")
            ),
            "cached_tokens": _as_int(
                payload.get("cached_tokens") or payload.get("cache_read_input_tokens")
            ),
            "reasoning_tokens": _as_int(
                payload.get("reasoning_tokens") or payload.get("thought_tokens")
            ),
            "total_tokens": _as_int(payload.get("total_tokens") or payload.get("tokens_used")),
        }
    return None


def _rows_from_threads_table(conn: sqlite3.Connection) -> Iterator[UsageRow]:
    try:
        cursor = conn.execute(
            "SELECT id, model_provider, model, tokens_used, created_at "
            "FROM threads WHERE tokens_used > 0"
        )
    except sqlite3.Error:
        return
    for ident, provider, model, tokens_used, created_at in cursor:
        ts = _iso_from_unix(created_at)
        tokens = _as_int(tokens_used)
        yield UsageRow(
            id=f"codex_thread:{ident}:{tokens}",
            ts=ts,
            date=_date_of(ts),
            source="codex_thread",
            grain="thread",
            provider=str(provider or "openai"),
            model=None if model is None else str(model),
            session=str(ident),
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            reasoning_tokens=None,
            total_tokens=tokens,
            dur_s=None,
            outcome=None,
            n_calls=1,
            proxy=False,
        )


def _infer_acpx_provider(session: dict) -> tuple[str, str | None]:
    argv = session.get("agent_argv") or []
    if not isinstance(argv, list):
        argv = []
    cmd = str(session.get("agent_command") or "")
    blob = " ".join(str(part) for part in argv) + " " + cmd
    model = None
    for index, arg in enumerate(argv):
        if str(arg) in {"--model", "-m"} and index + 1 < len(argv):
            model = str(argv[index + 1])
            break
    lowered = blob.lower()
    name = str(session.get("name") or "").lower()
    if "grok" in lowered or (model and "grok" in model.lower()):
        return "grok", model
    if "kimi" in lowered or (model and "kimi" in model.lower()):
        return "kimi", model
    if "luna" in name or "sol" in name or "codex" in lowered:
        return "acpx", model
    return "acpx", model


def _message_chars(session: dict) -> tuple[int | None, int | None]:
    messages = session.get("messages")
    if not isinstance(messages, list):
        return None, None
    prompt = 0
    response = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or message.get("type") or "").lower()
        text = message.get("content") if "content" in message else message.get("text")
        if isinstance(text, list):
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in text
            )
        if text is None:
            continue
        length = len(str(text))
        if role in {"user", "human", "prompt"}:
            prompt += length
        elif role in {"assistant", "model", "agent"}:
            response += length
    if prompt == 0 and response == 0:
        return None, None
    return prompt, response


def extract_acpx_session_rows(sessions_dir: Path) -> Iterator[UsageRow]:
    if not sessions_dir.is_dir():
        return
    for path in sorted(sessions_dir.glob("*.json")):
        if path.name in {"index.json"} or path.suffix in {".lock", ".tmp"}:
            continue
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(session, dict):
            continue
        provider, model = _infer_acpx_provider(session)
        ts = str(session.get("created_at") or session.get("updated_at") or "")
        name = session.get("name")
        session_name = None if name is None else str(name)
        requests = session.get("request_token_usage") or {}
        chars_prompt, chars_response = (None, None)
        if not requests:
            chars_prompt, chars_response = _message_chars(session)
        if isinstance(requests, dict) and requests:
            for request_id, usage in requests.items():
                if not isinstance(usage, dict):
                    continue
                yield UsageRow(
                    id=f"acpx_request:{path.stem}:{request_id}",
                    ts=ts,
                    date=_date_of(ts),
                    source="acpx_request",
                    grain="request",
                    provider=provider,
                    model=model,
                    session=session_name,
                    input_tokens=_as_int(usage.get("input_tokens")),
                    output_tokens=_as_int(usage.get("output_tokens")),
                    cached_tokens=_as_int(
                        usage.get("cache_read_input_tokens") or usage.get("cached_tokens")
                    ),
                    reasoning_tokens=_as_int(
                        usage.get("thought_tokens") or usage.get("reasoning_tokens")
                    ),
                    total_tokens=_as_int(usage.get("total_tokens")),
                    dur_s=None,
                    outcome=None,
                    n_calls=1,
                    proxy=False,
                )
            continue
        if chars_prompt or chars_response:
            yield UsageRow(
                id=f"acpx_chars:{path.stem}",
                ts=ts,
                date=_date_of(ts),
                source="acpx_request",
                grain="session",
                provider=provider,
                model=model,
                session=session_name,
                input_tokens=None,
                output_tokens=None,
                cached_tokens=None,
                reasoning_tokens=None,
                total_tokens=None,
                dur_s=None,
                outcome=None,
                n_calls=1,
                proxy=True,
                chars_prompt=chars_prompt,
                chars_response=chars_response,
            )


def _last_grok_usage(updates_path: Path) -> tuple[dict | None, str | None]:
    last_usage: dict | None = None
    last_ts: str | None = None
    try:
        handle = updates_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    with handle:
        for line in handle:
            if "inputTokens" not in line and '"usage"' not in line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = (
                (payload.get("params") or {}).get("update", {}).get("usage")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(usage, dict):
                continue
            last_usage = usage
            ts = payload.get("timestamp")
            last_ts = None if ts is None else str(ts)
    return last_usage, last_ts


def extract_grok_session_rows(homes: Iterable[Path]) -> Iterator[UsageRow]:
    for home in homes:
        sessions = home / "sessions"
        if not sessions.is_dir():
            continue
        for updates in sessions.rglob("updates.jsonl"):
            usage, usage_ts = _last_grok_usage(updates)
            if not usage:
                continue
            session_id = updates.parent.name
            summary_path = updates.parent / "summary.json"
            model = None
            created = usage_ts
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                summary = {}
            if isinstance(summary, dict):
                model = summary.get("current_model_id") or summary.get("primaryModelId")
                created = str(summary.get("created_at") or created or "")
            model_usage = usage.get("modelUsage")
            if model is None and isinstance(model_usage, dict) and model_usage:
                model = next(iter(model_usage))
            total = _as_int(usage.get("totalTokens"))
            yield UsageRow(
                id=f"grok_session:{session_id}:{total}",
                ts=str(created or ""),
                date=_date_of(str(created or "")),
                source="grok_session",
                grain="session",
                provider="grok",
                model=None if model is None else str(model),
                session=session_id,
                input_tokens=_as_int(usage.get("inputTokens")),
                output_tokens=_as_int(usage.get("outputTokens")),
                cached_tokens=_as_int(usage.get("cachedReadTokens")),
                reasoning_tokens=_as_int(usage.get("reasoningTokens")),
                total_tokens=total,
                dur_s=(
                    None
                    if usage.get("apiDurationMs") is None
                    else _as_float(usage.get("apiDurationMs")) / 1000.0
                    if _as_float(usage.get("apiDurationMs")) is not None
                    else None
                ),
                outcome=None,
                n_calls=_as_int(usage.get("modelCalls")) or 1,
                proxy=False,
            )


def extract_acpx_call_proxy_rows(log_path: Path) -> Iterator[UsageRow]:
    if not log_path.is_file():
        return
    try:
        handle = log_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for index, line in enumerate(handle):
            match = _ACPX_CALL.match(line)
            if not match:
                continue
            digest = hashlib.sha1(f"{index}:{line}".encode("utf-8", "replace")).hexdigest()
            date = match["date"]
            yield UsageRow(
                id=f"acpx_call:{digest}",
                ts=date,
                date=date,
                source="acpx_call_proxy",
                grain="call",
                provider=match["provider"],
                model=None,
                session=match["session"],
                input_tokens=None,
                output_tokens=None,
                cached_tokens=None,
                reasoning_tokens=None,
                total_tokens=None,
                dur_s=float(match["dur"]),
                outcome=match["outcome"],
                n_calls=1,
                proxy=True,
            )


def iter_all_rows(
    *,
    codex_home: Path,
    acpx_sessions: Path,
    grok_homes: Iterable[Path],
    console_log: Path,
) -> Iterator[UsageRow]:
    yield from extract_sqlite_rows(codex_home)
    yield from extract_acpx_session_rows(acpx_sessions)
    yield from extract_grok_session_rows(grok_homes)
    yield from extract_acpx_call_proxy_rows(console_log)


def append_rows(rows: Iterable[UsageRow], output_dir: Path, existing: set[str]) -> dict[str, object]:
    appended = 0
    skipped = 0
    by_source: dict[str, int] = {}
    handles: dict[str, object] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        for row in rows:
            if row.id in existing:
                skipped += 1
                continue
            month = row.date[:7] if len(row.date) >= 7 and row.date[4] == "-" else "unknown"
            path = output_dir / f"{month}.jsonl"
            handle = handles.get(month)
            if handle is None:
                handle = path.open("a", encoding="utf-8")
                handles[month] = handle
            handle.write(json.dumps(row.to_json(), ensure_ascii=False) + "\n")
            existing.add(row.id)
            appended += 1
            by_source[row.source] = by_source.get(row.source, 0) + 1
    finally:
        for handle in handles.values():
            handle.close()
    return {"appended": appended, "skipped": skipped, "by_source": by_source}


def sqlite_has_per_call_tokens(codex_home: Path) -> bool:
    for path in sorted(codex_home.glob("logs_*.sqlite")):
        report = inspect_sqlite(path)
        tables = report.get("tables") or {}
        logs = tables.get("logs") if isinstance(tables, dict) else None
        if isinstance(logs, dict) and logs.get("token_columns"):
            return True
    return False


def extract_and_append(
    *,
    repo_root: Path | None = None,
    state_dir: Path | None = None,
    codex_home: Path | None = None,
    acpx_sessions: Path | None = None,
    grok_homes: Iterable[Path] | None = None,
    console_log: Path | None = None,
) -> dict[str, object]:
    root = repo_root or REPO_ROOT
    state = state_dir or (root / "state")
    home = codex_home or (root / "ops" / "codex-home")
    sessions = acpx_sessions or DEFAULT_ACPX_SESSIONS
    grok = list(grok_homes) if grok_homes is not None else [
        root / "ops" / "grok-home",
        root / "ops" / "grok-home-medium",
    ]
    log_path = console_log or (state / "daemon_console.log")
    output_dir = usage_output_dir(state)
    existing = load_existing_ids(output_dir)
    per_call = sqlite_has_per_call_tokens(home)
    written = append_rows(
        iter_all_rows(
            codex_home=home,
            acpx_sessions=sessions,
            grok_homes=grok,
            console_log=log_path,
        ),
        output_dir,
        existing,
    )
    return {
        "output_dir": str(output_dir),
        "sqlite_has_per_call_tokens": per_call,
        "exact_cost_available": False,
        **written,
    }


def load_usage_rows(output_dir: Path) -> list[dict]:
    rows: list[dict] = []
    if not output_dir.is_dir():
        return rows
    for path in sorted(output_dir.glob("*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def percentile(values: list[int], q: float) -> int | None:
    """Nearest-rank percentile, entier (p50/p95 lisibles)."""

    if not values:
        return None
    ordered = sorted(int(v) for v in values)
    if q <= 0:
        return ordered[0]
    if q >= 100:
        return ordered[-1]
    index = math.ceil((q / 100.0) * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--codex-home", type=Path, default=None)
    parser.add_argument("--acpx-sessions", type=Path, default=None)
    parser.add_argument("--grok-home", type=Path, action="append", default=None)
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--inspect-sqlite", action="store_true")
    args = parser.parse_args(argv)
    root = REPO_ROOT
    if args.inspect_sqlite:
        home = args.codex_home or (root / "ops" / "codex-home")
        json.dump(
            [inspect_sqlite(path) for path in _iter_sqlite_homes(home)],
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 0
    report = extract_and_append(
        repo_root=root,
        state_dir=args.state_dir,
        codex_home=args.codex_home,
        acpx_sessions=args.acpx_sessions,
        grok_homes=args.grok_home,
        console_log=args.log,
    )
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
