from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore
from trader.infrastructure.state_db.world_knowledge_store import WorldKnowledgeStore
from trader.runtime.world_about_runtime import AboutForwardRunner, sweep_about_forward

CONFIG_DIR = REPO_ROOT / "config"
NOW = datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)


def _brief_payload(symbol: str = "1301.TW") -> dict[str, object]:
    return {
        "brief_id": f"company_micro:v1:{symbol}:abc",
        "symbol": symbol,
        "as_of": "2026-09-05T17:26:49+00:00",
        "input_signature": "0" * 64,
        "depth": "screen",
        "issuer_identity": {"issuer_name": "FP", "exchange": "TAI", "identity_status": "verified"},
        "coverage": {"status": "full"},
        "company_thesis": {"status": "watch"},
        "source_refs": ["s1"],
    }


def _write_briefs(state_dir: Path, symbols: tuple[str, ...] = ("1301.TW",)) -> None:
    from trader.infrastructure.state_db.fundamental_item_store import symbol_storage_key

    current = state_dir / "company_intelligence" / "current"
    current.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        payload = {"briefs": {"screen": _brief_payload(symbol)}}
        (current / f"{symbol_storage_key(symbol)}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_notes(state_dir: Path, rows: list[dict[str, object]]) -> None:
    db_path = state_dir / "situation_memory.db"
    store = SituationMemoryStore(db_path)
    store.close()
    conn = sqlite3.connect(db_path)
    try:
        for row in rows:
            conn.execute(
                """
                INSERT INTO situation_notes (
                    note_key, brief_id, as_of, valid_from, valid_until, point,
                    source_uuids, source_names, symbols, severity, signal,
                    direction, horizon, event_class
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["note_key"],
                    row["brief_id"],
                    row["as_of"],
                    row["valid_from"],
                    row["valid_until"],
                    row["point"],
                    json.dumps(row["source_uuids"]),
                    json.dumps(row["source_names"]),
                    json.dumps(row["symbols"]),
                    row["severity"],
                    row["signal"],
                    row["direction"],
                    row["horizon"],
                    row["event_class"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _note(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "note_key": "brief-1|symbol|1301.TW|0",
        "brief_id": "brief-1",
        "point": "Board declares a quarterly dividend.",
        "symbols": ["1301.TW"],
        "source_uuids": ["uuid-1"],
        "source_names": ["yahoo"],
        "severity": "info",
        "signal": "weak",
        "horizon": "next month",
        "direction": "bullish",
        "event_class": "capital",
        "as_of": "2026-09-10T12:00:00+00:00",
        "valid_from": "2026-09-10T12:00:00+00:00",
        "valid_until": "2026-09-11T08:00:00+00:00",
    }
    values.update(overrides)
    return values


def test_sweep_writes_news_and_company_then_reuses(tmp_path: Path) -> None:
    _write_briefs(tmp_path)
    _write_notes(tmp_path, [_note()])

    first = sweep_about_forward(config_dir=CONFIG_DIR, state_dir=tmp_path, now=NOW)
    assert first["status"] == "ok"
    assert first["notes_source"] == "read"
    assert first["notes"] == 1
    assert first["company_briefs"] == 1
    news = first["news"]
    company = first["company"]
    assert news["drafts"] == 1
    assert news["relations_linked"] == 1
    assert news["artifacts_appended"] == 1
    assert news["artifacts_reused"] == 0
    assert news["excluded"] == {}
    assert company["drafts"] == 1
    assert company["relations_linked"] == 2
    assert company["artifacts_appended"] == 1
    assert company["artifacts_reused"] == 0
    assert company["excluded"] == {}
    assert first["revision_id"] == first["company_revision_id"]
    assert str(first["revision_id"]).startswith("market_ontology:")
    assert len(WorldKnowledgeStore(tmp_path / "world_knowledge").load_corpus()) == 2

    second = sweep_about_forward(config_dir=CONFIG_DIR, state_dir=tmp_path, now=NOW)
    assert second["status"] == "ok"
    assert second["news"]["artifacts_appended"] == 0
    assert second["news"]["artifacts_reused"] == 1
    assert second["company"]["artifacts_appended"] == 0
    assert second["company"]["artifacts_reused"] == 1
    assert second["revision_id"] == first["revision_id"]
    assert len(WorldKnowledgeStore(tmp_path / "world_knowledge").load_corpus()) == 2


def test_sweep_without_notes_db_runs_company_only(tmp_path: Path) -> None:
    _write_briefs(tmp_path)

    report = sweep_about_forward(config_dir=CONFIG_DIR, state_dir=tmp_path, now=NOW)
    assert report["status"] == "ok"
    assert report["notes_source"] == "missing"
    assert report["news"]["drafts"] == 0
    assert report["company"]["drafts"] == 1
    assert report["revision_id"] == report["company_revision_id"]


def test_runner_trigger_sweeps_async_and_reports(tmp_path: Path) -> None:
    _write_briefs(tmp_path)
    _write_notes(tmp_path, [_note()])
    runner = AboutForwardRunner(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        min_interval_s=0,
        clock=lambda: NOW,
    )
    try:
        started = runner.trigger(reason="company_brief_written")
        assert started["triggered"] is True
        started["_thread"].join(timeout=60.0)
        assert started["_thread"].is_alive() is False
        status = runner.status()
        assert status["status"] == "ok"
        assert status["trigger"] == "company_brief_written"
        assert status["news"]["drafts"] == 1
        assert status["company"]["drafts"] == 1
        assert status["running"] is False
    finally:
        runner.stop()


def _publish_live_tip(config_dir: Path, state_dir: Path):
    from trader.application.world_model.issuer_registry import build_issuer_registry
    from trader.application.world_model.ontology_bootstrap import WorldOntologyAttestation
    from trader.application.world_model.world_scope_resolver import WorldScopeResolver
    from trader.infrastructure.files.company_briefs import load_company_briefs
    from trader.infrastructure.files.family_catalog_config import load_family_catalog
    from trader.infrastructure.state_db.world_graph_store import WorldGraphStore

    mapping = WorldScopeResolver.load(config_dir).mapping
    catalog = load_family_catalog(config_dir)
    corpus = load_company_briefs(state_dir / "company_intelligence")
    registry = build_issuer_registry(corpus.briefs, mapping).registry
    store = WorldGraphStore(state_dir / "world_model.db")
    try:
        return WorldOntologyAttestation(
            store, mapping, issuer_registry=registry, family_catalog=catalog
        ).ensure_published()
    finally:
        store.close()


def test_runner_full_sweep_bypasses_throttle_on_tip_change(tmp_path: Path) -> None:
    _write_briefs(tmp_path, ("1301.TW",))
    _write_notes(tmp_path, [_note()])
    runner = AboutForwardRunner(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        min_interval_s=3600,
        full_sweep_interval_s=3600,
        clock=lambda: NOW,
    )
    try:
        boot = runner.trigger(reason="daemon_boot")
        boot["_thread"].join(timeout=60.0)
        assert runner.status()["mode"] == "full"
        first_revision = runner.status()["revision_id"]
        _write_briefs(tmp_path, ("1301.TW", "1303.TW"))
        readiness = _publish_live_tip(CONFIG_DIR, tmp_path)
        assert readiness.reason == "superseded"
        assert readiness.revision_id != first_revision
        second = runner.trigger(reason="news_briefs_written")
        second["_thread"].join(timeout=60.0)
        status = runner.status()
        assert status["status"] == "ok"
        assert status["mode"] == "full"
        assert status["revision_id"] == readiness.revision_id
        assert status["revision_id"] != first_revision
    finally:
        runner.stop()


def test_runner_chains_full_sweep_when_fast_path_advances_tip(tmp_path: Path) -> None:
    _write_briefs(tmp_path, ("1301.TW",))
    _write_notes(tmp_path, [_note()])
    runner = AboutForwardRunner(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        min_interval_s=3600,
        full_sweep_interval_s=3600,
        clock=lambda: NOW,
    )
    try:
        boot = runner.trigger(reason="daemon_boot")
        boot["_thread"].join(timeout=60.0)
        assert runner.status()["mode"] == "full"
        first_revision = runner.status()["revision_id"]
        _write_briefs(tmp_path, ("1301.TW", "1303.TW"))
        named = runner.trigger(reason="company_brief_written", symbols=["1301.TW"])
        named["_thread"].join(timeout=60.0)
        status = runner.status()
        assert status["status"] == "ok"
        assert status["mode"] == "full"
        assert status["trigger"] == "company_brief_written+tip_advanced"
        assert status["revision_id"] != first_revision
    finally:
        runner.stop()


def test_runner_throttles_frequent_triggers(tmp_path: Path) -> None:
    _write_briefs(tmp_path)
    runner = AboutForwardRunner(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        min_interval_s=3600,
        clock=lambda: NOW,
    )
    try:
        first = runner.trigger(reason="first")
        assert first["triggered"] is True
        first["_thread"].join(timeout=60.0)
        assert runner.status()["status"] == "ok"
        second = runner.trigger(reason="second")
        assert second["triggered"] is True
        second["_thread"].join(timeout=60.0)
        status = runner.status()
        assert status["status"] == "skipped"
        assert status["reason"] == "throttled"
        assert status["trigger"] == "second"
    finally:
        runner.stop()


def test_runner_trigger_respects_disabled_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CASYS_WORLD_ABOUT_FORWARD_ENABLED", "0")
    runner = AboutForwardRunner(config_dir=CONFIG_DIR, state_dir=tmp_path)
    try:
        assert runner.trigger(reason="x") == {"triggered": False, "reason": "disabled"}
    finally:
        runner.stop()


def test_runner_sweep_failure_is_fail_open(tmp_path: Path) -> None:
    runner = AboutForwardRunner(
        config_dir=tmp_path / "nope",
        state_dir=tmp_path,
        min_interval_s=0,
    )
    try:
        started = runner.trigger(reason="bad_config")
        assert started["triggered"] is True
        started["_thread"].join(timeout=60.0)
        status = runner.status()
        assert status["status"] == "error"
        assert status["trigger"] == "bad_config"
        assert status["error"]
    finally:
        runner.stop()


def test_runner_stop_is_idempotent(tmp_path: Path) -> None:
    runner = AboutForwardRunner(config_dir=CONFIG_DIR, state_dir=tmp_path)
    runner.stop()
    runner.stop()
    assert runner.status()["stopping"] is True


def test_sweep_fast_writes_only_named_symbol(tmp_path: Path) -> None:
    _write_briefs(tmp_path, ("1301.TW", "1303.TW"))
    _write_notes(tmp_path, [_note()])

    fast = sweep_about_forward(
        config_dir=CONFIG_DIR, state_dir=tmp_path, now=NOW, only_symbols=["1301.TW"]
    )
    assert fast["status"] == "ok"
    assert fast["mode"] == "fast"
    assert fast["company"]["drafts"] == 1
    assert fast["company"]["relations_linked"] == 2
    assert fast["news"]["drafts"] == 0
    assert fast["notes_source"] == "not_requested"
    assert fast["skipped_symbols"] == {}
    assert len(WorldKnowledgeStore(tmp_path / "world_knowledge").load_corpus()) == 1

    full = sweep_about_forward(config_dir=CONFIG_DIR, state_dir=tmp_path, now=NOW)
    assert full["mode"] == "full"
    assert full["company"]["drafts"] == 2
    assert full["news"]["drafts"] == 1
    assert full["company"]["artifacts_reused"] == 1
    assert full["company"]["artifacts_appended"] == 1


def test_sweep_fast_news_by_brief_id_and_skips_unknown(tmp_path: Path) -> None:
    _write_briefs(tmp_path)
    _write_notes(
        tmp_path,
        [
            _note(),
            _note(
                note_key="brief-2|symbol|1301.TW|0",
                brief_id="brief-2",
                point="Analyst upgrades the name to overweight.",
            ),
        ],
    )

    fast = sweep_about_forward(
        config_dir=CONFIG_DIR, state_dir=tmp_path, now=NOW, only_brief_ids=["brief-2", "nob"]
    )
    assert fast["status"] == "ok"
    assert fast["mode"] == "fast"
    assert fast["news"]["drafts"] == 1
    assert fast["company"]["drafts"] == 0
    assert fast["skipped_brief_ids"] == {"nob": "no_notes"}
    assert fast["skipped_symbols"] == {}

    unknown = sweep_about_forward(
        config_dir=CONFIG_DIR, state_dir=tmp_path, now=NOW, only_symbols=["NOPE"]
    )
    assert unknown["status"] == "ok"
    assert unknown["company"]["drafts"] == 0
    assert unknown["skipped_symbols"] == {"NOPE": "brief_file_missing"}


def test_runner_fast_path_runs_unthrottled_after_full(tmp_path: Path) -> None:
    _write_briefs(tmp_path, ("1301.TW", "1303.TW"))
    runner = AboutForwardRunner(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        min_interval_s=3600,
        full_sweep_interval_s=3600,
        clock=lambda: NOW,
    )
    try:
        boot = runner.trigger(reason="daemon_boot")
        assert boot["triggered"] is True
        boot["_thread"].join(timeout=60.0)
        assert runner.status()["mode"] == "full"
        assert runner.status()["company"]["drafts"] == 2
        item = runner.trigger(reason="company_brief_written", symbols=["1301.TW"])
        assert item["triggered"] is True
        item["_thread"].join(timeout=60.0)
        status = runner.status()
        assert status["status"] == "ok"
        assert status["mode"] == "fast"
        assert status["company"]["drafts"] == 1
        assert status["company"]["artifacts_reused"] == 1
    finally:
        runner.stop()


def test_runner_interval_upgrades_item_trigger_to_full(tmp_path: Path) -> None:
    _write_briefs(tmp_path)
    _write_notes(tmp_path, [_note()])
    tick = {"now": 1000.0}
    runner = AboutForwardRunner(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        min_interval_s=0,
        full_sweep_interval_s=60,
        clock=lambda: NOW,
        now_fn=lambda: tick["now"],
    )
    try:
        first = runner.trigger(reason="daemon_boot")
        first["_thread"].join(timeout=60.0)
        assert runner.status()["mode"] == "full"
        tick["now"] += 61.0
        second = runner.trigger(reason="company_brief_written", symbols=["1301.TW"])
        second["_thread"].join(timeout=60.0)
        status = runner.status()
        assert status["mode"] == "full"
        assert status["news"]["drafts"] == 1
    finally:
        runner.stop()


def test_merge_pending_unions_items_and_marks_full() -> None:
    from trader.runtime.world_about_runtime import _merge_pending, _PendingSweep

    symbols, brief_ids, full = _merge_pending(
        [
            _PendingSweep(reason="a", symbols=["AAA", "AAA"], brief_ids=["b1"]),
            _PendingSweep(reason="b", symbols=["BBB"], brief_ids=["b1", "b2"]),
        ]
    )
    assert symbols == ("AAA", "BBB")
    assert brief_ids == ("b1", "b2")
    assert full is False
    _, _, full = _merge_pending([_PendingSweep(reason="boot")])
    assert full is True
    symbols, brief_ids, full = _merge_pending([_PendingSweep(reason="x", symbols=["  "])])
    assert (symbols, brief_ids, full) == ((), (), False)


def test_runner_writes_status_file(tmp_path: Path) -> None:
    import json

    _write_briefs(tmp_path)
    runner = AboutForwardRunner(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        min_interval_s=0,
        clock=lambda: NOW,
    )
    try:
        started = runner.trigger(reason="daemon_boot")
        started["_thread"].join(timeout=60.0)
        payload = json.loads((tmp_path / "world_about_runner_status.json").read_text(encoding="utf-8"))
        assert payload["status"] == "ok"
        assert payload["last"]["mode"] == "full"
        assert payload["pid"] > 0
        assert payload["started_at"] <= payload["updated_at"]
    finally:
        runner.stop()


def test_daemon_about_extractors_never_raise() -> None:
    from trader.runtime.daemon import _about_brief_ids, _about_symbol

    assert _about_brief_ids(({"venue": "US", "brief_ref": {"brief_id": "b1"}}, {"venue": "X"})) == ("b1",)
    assert _about_brief_ids(None) == ()
    assert _about_brief_ids(({"brief_ref": "nope"}, 42)) == ()
    assert _about_symbol({"symbol": "AAA", "brief_ref": {}}) == ("AAA",)
    assert _about_symbol({}) == ()
    assert _about_symbol(None) == ()
    assert _about_symbol({"symbol": "  "}) == ()
