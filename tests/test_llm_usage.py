"""Commandes llm_usage, y compris la vue cost/latence."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import llm_cost, llm_usage


def test_overview_et_daily_restent_stables(tmp_path: Path, capsys) -> None:
    log = tmp_path / "daemon_console.log"
    log.write_text(
        "2026-08-13 09:38:19,068 INFO [acpx_call] session=9334:0 symbol=- "
        "pid=1 provider=acpx timeout_s=255 dur_s=2.0 outcome=ok\n"
        "2026-08-13 10:00:00,000 INFO [acpx_call] session=1:0 symbol=- "
        "pid=2 provider=universe timeout_s=30 dur_s=1.0 outcome=ok\n",
        encoding="utf-8",
    )
    llm_usage.main(["--log", str(log), "overview"])
    out = capsys.readouterr().out
    assert "acpx" in out
    assert "universe" in out
    llm_usage.main(["--log", str(log), "daily"])
    daily = capsys.readouterr().out
    assert "2026-08-13" in daily


def test_cost_agrege_appels_tokens_et_p50_p95(tmp_path: Path, capsys) -> None:
    log = tmp_path / "daemon_console.log"
    log.write_text(
        "2026-08-16 01:00:00,000 INFO [acpx_call] session=1:0 symbol=- "
        "pid=1 provider=acpx timeout_s=30 dur_s=4.0 outcome=ok\n"
        "2026-08-16 01:01:00,000 INFO [acpx_call] session=1:0 symbol=- "
        "pid=2 provider=acpx timeout_s=30 dur_s=6.0 outcome=ok\n",
        encoding="utf-8",
    )
    usage_dir = tmp_path / "archive" / "llm_usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "2026-08.jsonl").write_text(
        json.dumps(
            {
                "id": "acpx_request:s:r1",
                "date": "2026-08-16",
                "source": "acpx_request",
                "grain": "request",
                "provider": "acpx",
                "input_tokens": 1000,
                "output_tokens": 100,
                "cached_tokens": 50,
                "total_tokens": 1150,
                "n_calls": 1,
                "proxy": False,
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "grok_session:sid:10",
                "date": "2026-08-16",
                "source": "grok_session",
                "grain": "session",
                "provider": "grok",
                "session": "sid",
                "total_tokens": 10,
                "n_calls": 1,
                "proxy": False,
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "codex_thread:thr-1:999999",
                "date": "2026-08-16",
                "source": "codex_thread",
                "grain": "thread",
                "provider": "openai",
                "session": "thr-1",
                "total_tokens": 999999,
                "n_calls": 1,
                "proxy": False,
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "grok_session:sid:20",
                "date": "2026-08-16",
                "source": "grok_session",
                "grain": "session",
                "provider": "grok",
                "session": "sid",
                "total_tokens": 20,
                "n_calls": 1,
                "proxy": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "event": "cycle_completed",
                "stage_timings_ms": {"snapshot_ms": 10, "decide_ms": 100, "total_ms": 200},
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "cycle_completed",
                "stage_timings_ms": {"snapshot_ms": 30, "decide_ms": 300, "total_ms": 400},
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "cycle_completed",
                "stage_timings_ms": {"snapshot_ms": 20, "decide_ms": 200, "total_ms": 300},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    llm_usage.main(
        [
            "--log",
            str(log),
            "--events",
            str(events),
            "--usage-dir",
            str(usage_dir),
            "cost",
            "--price-per-mtok",
            "acpx=2",
        ]
    )
    out = capsys.readouterr().out
    assert "2026-08-16" in out
    assert "acpx" in out
    assert "1 150" in out or "1150" in out
    assert "0.00" in out  # 1150 tokens * $2 / 1e6
    assert "grok" in out
    assert "20" in out  # deduped session keeps max total
    assert "openai" not in out  # thread-lifetime totals are not daily cost
    assert "snapshot_ms" in out
    assert "decide_ms" in out
    assert llm_cost.percentile([10, 20, 30], 50) == 20
    assert llm_cost.percentile([10, 20, 30], 95) == 30
