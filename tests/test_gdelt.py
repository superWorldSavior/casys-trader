"""Tests offline du collecteur GDELT bulk (seam get_bytes injectable, zéro réseau)."""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from datetime import datetime, timezone

from trader.infrastructure.market_sources import gdelt

_NOW = datetime(2026, 9, 17, 12, 7, tzinfo=timezone.utc)


def _gkg_row(
    url: str,
    themes: str,
    *,
    date: str = "20260917090000",
    source: str = "reuters.com",
    tone: str = "-2.1,5,1,0",
) -> str:
    cols = [""] * 16
    cols[1] = date
    cols[3] = source
    cols[4] = url
    cols[7] = themes
    cols[15] = tone
    return "\t".join(cols)


def _gkg_bytes(*rows: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("20260917090000.gkg.csv", "\n".join(rows) + "\n")
    return buffer.getvalue()


def _fake_chunks() -> dict:
    return {
        "central": _gkg_bytes(
            _gkg_row("https://a.example/1", "CENTRAL_BANK;MONETARY_POLICY"),
            _gkg_row("https://b.example/2", "SANCTIONS;TRADE"),
            _gkg_row("https://a.example/1", "CENTRAL_BANK"),
            _gkg_row("", "WAR"),
            _gkg_row("https://c.example/3", "SPORTS;WEATHER"),
            _gkg_row("https://f.example/4", "REWARD_POINTS;FORWARD_GUIDANCE"),
            "short\tline",
        ),
    }


def _get_bytes(chunks: dict):
    def fetch(url: str) -> bytes:
        return chunks["central"]

    return fetch


def test_collect_filters_dedupes_and_drops_urlless(tmp_path):
    result = gdelt.collect_daily(tmp_path, _NOW, get_bytes=_get_bytes(_fake_chunks()))
    # 8 chunks identiques : 2 lignes uniques + 1 doublon intra-chunk + 21 doublons inter-chunks.
    assert result == {"collected": 2, "skipped": 22, "errors": 0}
    lines = (tmp_path / "gdelt" / "events.jsonl").read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["url"] == "https://a.example/1"
    assert first["themes"] == ["CENTRAL_BANK", "MONETARY_POLICY"]
    assert first["tone"] == -2.1
    assert first["seendate"] == "20260917T090000Z"
    assert first["source"] == "gdelt_bulk"
    assert "title" not in first


def test_collect_is_idempotent_across_runs(tmp_path):
    get = _get_bytes(_fake_chunks())
    gdelt.collect_daily(tmp_path, _NOW, get_bytes=get)
    second = gdelt.collect_daily(tmp_path, _NOW, get_bytes=get)
    assert second["collected"] == 0
    # 3 lignes matchées (a1, b2, a1) × 8 chunks, toutes déjà collectées.
    assert second["skipped"] == 24
    assert second["errors"] == 0


def test_collect_dedupes_same_url_across_chunks(tmp_path):
    same = _gkg_bytes(_gkg_row("https://d.example/9", "ELECTION;GEOPOLITICAL"))
    result = gdelt.collect_daily(tmp_path, _NOW, get_bytes=lambda url: same)
    assert result["collected"] == 1
    assert result["skipped"] == 7


def test_chunk_failure_is_fail_soft_per_chunk(tmp_path):
    calls = {"n": 0}

    def flaky(url: str) -> bytes:
        calls["n"] += 1
        if calls["n"] % 2:
            raise urllib.error.URLError("network down")
        return _gkg_bytes(_gkg_row("https://e.example/1", "WAR"))

    result = gdelt.collect_daily(tmp_path, _NOW, get_bytes=flaky)
    assert result["collected"] == 1
    assert result["errors"] == 4


def test_malformed_chunk_is_fail_soft(tmp_path):
    result = gdelt.collect_daily(tmp_path, _NOW, get_bytes=lambda url: b"not a zip")
    assert result == {"collected": 0, "skipped": 0, "errors": 8}


def test_chunk_stamps_spread_over_24h():
    stamps = gdelt._chunk_stamps(_NOW)
    assert len(stamps) == 8
    assert stamps[0] == "20260917120000"
    assert stamps[-1] == "20260916150000"
    assert len(set(stamps)) == 8


def test_maybe_collect_respects_cooldown(tmp_path):
    marker = tmp_path / "gdelt" / gdelt.MARKER_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(_NOW.isoformat(), encoding="utf-8")
    result = gdelt.maybe_collect(tmp_path, _NOW, get_bytes=_get_bytes(_fake_chunks()))
    assert result["triggered"] is False
    assert result["reason"] == "cooldown"


def test_maybe_collect_triggers_and_writes(tmp_path):
    result = gdelt.maybe_collect(tmp_path, _NOW, get_bytes=_get_bytes(_fake_chunks()))
    assert result["triggered"] is True
    result["_thread"].join(timeout=5)
    lines = (tmp_path / "gdelt" / "events.jsonl").read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
