"""Socle unique : append d'une ligne dans le JSONL du jour + projection latest.

Le prochain artefact d'intelligence passe par ``JsonlDayLedger.write_session``.
La politique latest (toujours remplacer, préserver un succès, skip) reste au
store : le socle n'en choisit aucune.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date as date_type
from pathlib import Path
from typing import Any, TextIO

from trader.infrastructure.state_db.shadow import write_json_atomic


def validated_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = date_type.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError("date must be YYYY-MM-DD")
    return text


def calendar_date_from_as_of(value: Any, *, empty_error: str) -> str:
    text = str(value)
    if len(text) < 10:
        raise ValueError(empty_error)
    return validated_date(text[:10])


def loose_date_from_as_of(value: Any, *, empty_error: str, strip: bool = True) -> str:
    as_of = str(value or "").strip() if strip else str(value)
    if len(as_of) >= 10 and as_of[4] == "-" and as_of[7] == "-":
        return as_of[:10]
    raise ValueError(empty_error)


def required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def safe_filename_component(
    value: str,
    *,
    empty: str | None = None,
    empty_error: str = "value does not contain a safe filename component",
) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in ("_", "-"))
    if safe:
        return safe
    if empty is not None:
        return empty
    raise ValueError(empty_error)


def jsonl_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def find_newest_matching(
    base_dir: Path,
    match: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    for path in sorted(base_dir.glob("????-??-??.jsonl"), reverse=True):
        for candidate in reversed(read_jsonl_objects(path)):
            if match(candidate):
                return candidate
    return None


def read_projection_or_scan(
    latest_path: Path,
    base_dir: Path,
    match: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    payload = read_json_object(latest_path)
    if payload is not None and match(payload):
        return payload
    return find_newest_matching(base_dir, match)


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def project_json(path: Path, payload: Mapping[str, Any]) -> None:
    write_json_atomic(path, payload if isinstance(payload, dict) else dict(payload))


def project_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(path, jsonl_dumps(payload) + "\n")


def _needs_line_separator(path: Path) -> bool:
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def append_jsonl_line(
    path: Path,
    payload: Mapping[str, Any],
    *,
    flock: bool = False,
    repair_missing_newline: bool = False,
    handle: TextIO | None = None,
) -> None:
    line = jsonl_dumps(payload) + "\n"
    prefix = "\n" if repair_missing_newline and _needs_line_separator(path) else ""
    if handle is not None:
        handle.write(prefix + line)
        handle.flush()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        if flock:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(prefix + line)
            if flock:
                fh.flush()
        finally:
            if flock:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class JsonlDayLedger:
    """JSONL quotidien + lock d'écriture. La projection latest est au store."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        validate_date: bool = True,
        use_fcntl: bool = False,
        repair_missing_newline: bool = False,
    ) -> None:
        self.base_dir = Path(base_dir)
        self._validate_date = validate_date
        self._use_fcntl = use_fcntl
        self._repair_missing_newline = repair_missing_newline
        self._lock = threading.RLock()

    def path_for_date(
        self,
        date: str,
        *,
        directory: Path | None = None,
        validate: bool | None = None,
    ) -> Path:
        should_validate = self._validate_date if validate is None else validate
        key = validated_date(date) if should_validate else str(date)
        return (directory if directory is not None else self.base_dir) / f"{key}.jsonl"

    @contextmanager
    def write_session(
        self,
        date: str | None = None,
        *,
        directory: Path | None = None,
        validate: bool | None = None,
    ) -> Iterator[_WriteSession]:
        with self._lock:
            session = _WriteSession(self, date, directory=directory, validate=validate)
            try:
                yield session
            finally:
                session.close()

    def append_if_changed(
        self,
        payload: Mapping[str, Any],
        *,
        date: str,
        latest_path: Path,
        identity_field: str,
        read_current: Callable[[], Mapping[str, Any] | None],
        on_unchanged: Callable[[], None] | None = None,
        after_write: Callable[[], None] | None = None,
        directory: Path | None = None,
        validate: bool | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self.write_session() as session:
            current = read_current()
            if current is not None and current.get(identity_field) == payload[identity_field]:
                if on_unchanged is not None:
                    on_unchanged()
                return dict(current), False
            session.append(payload, date=date, directory=directory, validate=validate)
            session.project_json(latest_path, payload)
            if after_write is not None:
                after_write()
            return payload if isinstance(payload, dict) else dict(payload), True


class _WriteSession:
    def __init__(
        self,
        ledger: JsonlDayLedger,
        date: str | None,
        *,
        directory: Path | None,
        validate: bool | None,
    ) -> None:
        self._ledger = ledger
        self._handle: TextIO | None = None
        if ledger._use_fcntl and date is not None:
            path = ledger.path_for_date(date, directory=directory, validate=validate)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._handle = handle

    def append(
        self,
        payload: Mapping[str, Any],
        *,
        date: str,
        directory: Path | None = None,
        validate: bool | None = None,
    ) -> None:
        path = self._ledger.path_for_date(date, directory=directory, validate=validate)
        append_jsonl_line(
            path,
            payload,
            repair_missing_newline=self._ledger._repair_missing_newline,
            handle=self._handle,
        )

    def project_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        project_json(path, payload)

    def project_jsonl(self, path: Path, payload: Mapping[str, Any]) -> None:
        project_jsonl(path, payload)

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
