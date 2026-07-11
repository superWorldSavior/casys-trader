"""Tests offline du client GDELT (seam get_json injectable, zéro réseau)."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

from trader.infrastructure.market_sources import gdelt

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _fake_response() -> dict:
    return {
        "articles": [
            {
                "url": "https://a.example/1",
                "title": "War escalates in region X",
                "seendate": "20260710T041500Z",
                "domain": "a.example",
                "sourcecountry": "China",
                "language": "Chinese",
            },
            {
                "url": "https://b.example/2",
                "title": "New sanctions announced",
                "seendate": "20260710T050000Z",
                "domain": "b.example",
                "sourcecountry": "United States",
                "language": "English",
            },
            {"url": "https://a.example/1", "title": "duplicate url"},
            {"title": "no url — dropped"},
        ]
    }


def test_collect_normalises_dedupes_and_drops_urlless(tmp_path):
    result = gdelt.collect_daily(tmp_path, _NOW, get_json=lambda url: _fake_response())
    assert result == {"collected": 2, "skipped": 1, "errors": 0}
    lines = (tmp_path / "gdelt" / "events.jsonl").read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["url"] == "https://a.example/1"
    assert set(first) >= {
        "ts_collected",
        "url",
        "title",
        "seendate",
        "domain",
        "sourcecountry",
        "language",
    }


def test_collect_is_idempotent_across_runs(tmp_path):
    gdelt.collect_daily(tmp_path, _NOW, get_json=lambda url: _fake_response())
    second = gdelt.collect_daily(tmp_path, _NOW, get_json=lambda url: _fake_response())
    assert second["collected"] == 0
    # Le batch contient 3 URLs valides (a1, b2, a1) toutes déjà collectées → 3 skips.
    assert second["skipped"] == 3


def test_fetch_error_is_fail_soft(tmp_path):
    def boom(url):
        raise urllib.error.URLError("network down")

    result = gdelt.collect_daily(tmp_path, _NOW, get_json=boom)
    assert result == {"collected": 0, "skipped": 0, "errors": 1}


def test_non_dict_payload_is_fail_soft(tmp_path):
    result = gdelt.collect_daily(tmp_path, _NOW, get_json=lambda url: ["not", "a", "dict"])
    assert result == {"collected": 0, "skipped": 0, "errors": 1}


def test_title_is_bounded(tmp_path):
    resp = {"articles": [{"url": "https://c.example/1", "title": "x" * 500}]}
    gdelt.collect_daily(tmp_path, _NOW, get_json=lambda url: resp)
    row = json.loads((tmp_path / "gdelt" / "events.jsonl").read_text("utf-8").strip())
    assert len(row["title"]) == 300


def test_maybe_collect_respects_cooldown(tmp_path):
    marker = tmp_path / "gdelt" / gdelt.MARKER_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(_NOW.isoformat(), encoding="utf-8")
    result = gdelt.maybe_collect(tmp_path, _NOW, get_json=lambda url: _fake_response())
    assert result["triggered"] is False
    assert result["reason"] == "cooldown"


def test_maybe_collect_triggers_and_writes(tmp_path):
    result = gdelt.maybe_collect(tmp_path, _NOW, get_json=lambda url: _fake_response())
    assert result["triggered"] is True
    result["_thread"].join(timeout=5)
    lines = (tmp_path / "gdelt" / "events.jsonl").read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
