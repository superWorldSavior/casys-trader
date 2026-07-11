from __future__ import annotations

import json
from pathlib import Path

from trader.infrastructure.files.model_performance_jsonl import JsonlModelPerformanceReader


def test_missing_model_performance_ledger_is_empty(tmp_path: Path) -> None:
    reader = JsonlModelPerformanceReader(tmp_path / "missing.jsonl")

    assert list(reader.read_rows()) == []


def test_reader_streams_valid_objects_and_ignores_malformed_or_incomplete_lines(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "model_performance.jsonl"
    expected = {"symbol": "SPY", "action": "BUY"}
    path.write_text(
        "\n".join(
            [
                json.dumps(expected),
                "not-json",
                json.dumps(["not", "an", "object"]),
                '{"symbol":"QQQ"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    def fail_read_text(*_args, **_kwargs):
        raise AssertionError("the JSONL adapter must stream instead of read_text")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    assert list(JsonlModelPerformanceReader(path).read_rows()) == [expected]
