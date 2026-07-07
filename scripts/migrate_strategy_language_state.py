#!/usr/bin/env python3
"""Migrate persisted state to the canonical strategy language names."""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

import json

from trader.application.migration.strategy_language_migration import (
    migrate_json_file,
    migrate_jsonl_file,
    migrate_strategy_language_value,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILES = (
    ROOT / "state" / "decisions.jsonl",
    ROOT / "state" / "current_report.json",
    ROOT / "state" / "last_report.json",
)


def _backup(path: Path, stamp: str) -> Path:
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def _jsonl_would_change(path: Path) -> bool:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        migrated = migrate_strategy_language_value(json.loads(line))
        if json.dumps(migrated, ensure_ascii=False, separators=(",", ":")) != line:
            return True
    return False


def _json_would_change(path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    return migrate_strategy_language_value(data) != data


def _migrate_path(path: Path, *, commit: bool, stamp: str) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    if not commit:
        if path.suffix == ".jsonl":
            return _jsonl_would_change(path), "dry-run"
        if path.suffix == ".json":
            return _json_would_change(path), "dry-run"
        return False, "unsupported"
    _backup(path, stamp)
    if path.suffix == ".jsonl":
        return migrate_jsonl_file(path) > 0, "jsonl"
    if path.suffix == ".json":
        return migrate_json_file(path), "json"
    return False, "unsupported"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="JSON/JSONL files to migrate")
    parser.add_argument("--commit", action="store_true", help="write changes; default is dry-run")
    args = parser.parse_args(argv)

    paths = args.paths or list(DEFAULT_FILES)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in paths:
        changed, kind = _migrate_path(path, commit=args.commit, stamp=stamp)
        status = "changed" if changed else "unchanged"
        print(f"{path}: {status} ({kind})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
