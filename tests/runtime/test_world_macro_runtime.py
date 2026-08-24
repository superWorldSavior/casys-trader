"""MACRO-6: single-flight fail-open source-only macro worker."""

from __future__ import annotations

import inspect
import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_macro import MacroCollectionTarget, MacroScope


UTC = timezone.utc
NOW = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture(autouse=True)
def _forbid_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("live network is forbidden in MACRO-6 runtime tests")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


def _dbnomics_body(period: str, value: float) -> str:
    return json.dumps(
        {
            "series": {
                "docs": [
                    {
                        "period": [period],
                        "value": [value],
                        "indexed_at": "2026-08-23T12:30:00Z",
                    }
                ]
            }
        }
    )


def _yahoo_body(period_date: str, close: float) -> str:
    import calendar
    from datetime import date

    ts = int(calendar.timegm(date.fromisoformat(period_date).timetuple()))
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "timestamp": [ts],
                        "meta": {"exchangeTimezoneName": "UTC"},
                        "indicators": {"quote": [{"close": [close]}]},
                    }
                ],
                "error": None,
            }
        }
    )


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


class ScriptedTransport:
    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> object:
        from trader.infrastructure.market_sources.world_macro.series import MacroHttpResponse

        del timeout_s, headers
        with self.lock:
            self.calls.append(url)
            if not self.script:
                raise AssertionError(f"unexpected extra HTTP call: {url}")
            item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return MacroHttpResponse(status=200, body=str(item), headers={})


def test_macro_lane_identity_is_distinct_from_market_and_context() -> None:
    from trader.runtime.world_macro_runtime import MACRO_LANE_IDENTITY, MACRO_THREAD_NAME

    assert MACRO_LANE_IDENTITY == "world.context.macro"
    assert MACRO_LANE_IDENTITY != "context.v1"
    assert MACRO_THREAD_NAME != "world-model-shadow"
    assert "macro" in MACRO_THREAD_NAME


def test_background_runner_is_single_flight_coalescent_and_non_blocking() -> None:
    from trader.runtime.world_macro_runtime import WorldMacroBackgroundRunner

    entered = threading.Event()
    release = threading.Event()
    calls: list[datetime] = []

    def collect_fn(*, now: datetime, reason: str) -> dict[str, object]:
        calls.append(now)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=2.0)
        return {"status": "ok", "reason": reason, "as_of": now.isoformat()}

    runner = WorldMacroBackgroundRunner(collect_fn=collect_fn)
    try:
        first = runner.trigger(now=NOW, reason="first")
        assert first["triggered"] is True
        assert first["reason"] == "first"
        assert entered.wait(timeout=1.0)
        second = runner.trigger(now=NOW + timedelta(minutes=4), reason="latest")
        assert second == {"triggered": False, "reason": "queued_latest"}
        release.set()
        first["_thread"].join(timeout=2.0)
        assert calls == [NOW, NOW + timedelta(minutes=4)]
        assert runner.status()["running"] is False
        assert runner.status()["pending"] is False
        again = runner.trigger(now=NOW + timedelta(minutes=8), reason="again")
        assert again["triggered"] is True
        again["_thread"].join(timeout=2.0)
    finally:
        runner.stop()


def test_background_runner_swallows_collect_crash_and_keeps_fail_open() -> None:
    from trader.runtime.world_macro_runtime import WorldMacroBackgroundRunner

    def collect_fn(*, now: datetime, reason: str) -> dict[str, object]:
        del now, reason
        raise OSError("macro disk unavailable")

    runner = WorldMacroBackgroundRunner(collect_fn=collect_fn)
    try:
        result = runner.trigger(now=NOW, reason="post_cycle")
        assert result["triggered"] is True
        result["_thread"].join(timeout=2.0)
        status = runner.status()
        assert status["status"] == "partial"
        assert status["errors"][0]["stage"] == "collect"
        assert "OSError" in str(status["errors"][0]["error"])
    finally:
        runner.stop()


def test_background_runner_stop_is_bounded_and_rejects_new_triggers() -> None:
    from trader.runtime.world_macro_runtime import WorldMacroBackgroundRunner

    runner = WorldMacroBackgroundRunner(collect_fn=lambda **_kwargs: {"status": "ok"})
    runner.stop()
    assert runner.trigger(now=NOW, reason="after_stop") == {"triggered": False, "reason": "stopping"}
    stop_source = inspect.getsource(runner.stop)
    assert "join(timeout=" in stop_source


def test_collect_runs_each_target_once_sequentially_without_worker_retries() -> None:
    from trader.runtime.world_macro_runtime import collect_world_macro

    targets = (
        MacroCollectionTarget(scope=MacroScope(kind="world", entity_id="market"), source_ids=("gold_usd",)),
        MacroCollectionTarget(
            scope=MacroScope(kind="country", entity_id="iso-3166:US"), source_ids=("fed_funds_effective",)
        ),
    )
    calls: list[MacroCollectionTarget] = []

    class Pipeline:
        def collect(self, **kwargs: object) -> SimpleNamespace:
            target = kwargs["target"]
            assert isinstance(target, MacroCollectionTarget)
            calls.append(target)
            assert kwargs["cutoff_at"] == NOW
            assert "scope" not in kwargs
            return SimpleNamespace(status="completed", run_id=f"run:{target.scope}")

    report = collect_world_macro(
        now=NOW,
        reason="post_cycle",
        pipeline=Pipeline(),  # type: ignore[arg-type]
        registry=SimpleNamespace(entries=()),
        sources={"fed_funds_effective": object()},
        targets=targets,
        budgets=SimpleNamespace(
            fetch_in_run_cycle=False,
            fetch_in_world_capture_worker=False,
            max_parallel_requests=1,
            retry_max=1,
            honor_retry_after=True,
        ),
    )
    assert calls == list(targets)
    assert report["status"] == "ok"
    assert report["lane_identity"] == "world.context.macro"
    assert report["authority"] == "shadow_only"
    assert report["decision_effect"] == "none"
    assert len(report["runs"]) == 2
    source = inspect.getsource(collect_world_macro)
    assert "retry" not in source.lower()
    assert "ThreadPool" not in source
    assert "concurrent" not in source


def test_target_failure_does_not_abort_remaining_targets_or_raise() -> None:
    from trader.runtime.world_macro_runtime import collect_world_macro

    targets = (
        MacroCollectionTarget(
            scope=MacroScope(kind="country", entity_id="iso-3166:US"), source_ids=("fed_funds_effective",)
        ),
        MacroCollectionTarget(scope=MacroScope(kind="world", entity_id="market"), source_ids=("gold_usd",)),
    )

    class Pipeline:
        def collect(self, **kwargs: object) -> SimpleNamespace:
            target = kwargs["target"]
            assert isinstance(target, MacroCollectionTarget)
            if target.scope.kind == "country":
                raise RuntimeError("http 429 too many requests")
            return SimpleNamespace(status="completed", run_id="run-world")

    report = collect_world_macro(
        now=NOW,
        reason="post_cycle",
        pipeline=Pipeline(),  # type: ignore[arg-type]
        registry=object(),
        sources={},
        targets=targets,
    )
    assert report["status"] == "partial"
    assert len(report["errors"]) == 1
    assert report["runs"][0]["status"] == "completed"
    assert report["runs"][0]["scope"]["kind"] == "world"


def test_failed_us_target_does_not_abort_remaining_typed_targets_and_report_is_honest() -> None:
    from trader.runtime.world_macro_runtime import collect_world_macro

    targets = (
        MacroCollectionTarget(
            scope=MacroScope(kind="country", entity_id="iso-3166:US"),
            source_ids=("cpi_us_imf", "fed_funds_effective", "unemployment_rate_us"),
        ),
        MacroCollectionTarget(
            scope=MacroScope(kind="region", entity_id="iso-un-m49:150"),
            source_ids=("ecb_deposit_rate", "hicp_euro_area"),
        ),
        MacroCollectionTarget(
            scope=MacroScope(kind="world", entity_id="market"),
            source_ids=("brent_crude_usd", "gold_usd"),
        ),
    )
    calls: list[tuple[str, str]] = []

    class Pipeline:
        def collect(self, **kwargs: object) -> SimpleNamespace:
            target = kwargs["target"]
            assert isinstance(target, MacroCollectionTarget)
            calls.append((target.scope.kind, target.scope.entity_id))
            if target.scope.kind == "country":
                return SimpleNamespace(status="failed", run_id="run-us", reason="no_admissible_observation")
            return SimpleNamespace(status="completed", run_id=f"run:{target.scope.kind}")

    report = collect_world_macro(
        now=NOW,
        reason="post_cycle",
        pipeline=Pipeline(),  # type: ignore[arg-type]
        registry=object(),
        sources={},
        targets=targets,
    )
    assert calls == [
        ("country", "iso-3166:US"),
        ("region", "iso-un-m49:150"),
        ("world", "market"),
    ]
    assert len(report["runs"]) == 3
    assert report["runs"][0]["status"] == "failed"
    assert report["runs"][1]["status"] == "completed"
    assert report["runs"][2]["status"] == "completed"
    assert report["status"] == "partial"


def test_all_failed_targets_report_failed_without_restamping() -> None:
    from trader.runtime.world_macro_runtime import collect_world_macro

    targets = (
        MacroCollectionTarget(scope=MacroScope(kind="country", entity_id="iso-3166:US"), source_ids=("cpi_us_imf",)),
        MacroCollectionTarget(scope=MacroScope(kind="world", entity_id="market"), source_ids=("gold_usd",)),
    )

    class Pipeline:
        def collect(self, **kwargs: object) -> SimpleNamespace:
            target = kwargs["target"]
            assert isinstance(target, MacroCollectionTarget)
            return SimpleNamespace(status="failed", run_id=f"run:{target.scope.kind}")

    report = collect_world_macro(
        now=NOW,
        reason="post_cycle",
        pipeline=Pipeline(),  # type: ignore[arg-type]
        registry=object(),
        sources={},
        targets=targets,
    )
    assert len(report["runs"]) == 2
    assert report["status"] == "failed"


def test_runtime_module_excludes_gdelt_news_macro_brief_and_run_cycle_fetch() -> None:
    from trader.runtime import world_macro_runtime

    source = Path(world_macro_runtime.__file__).read_text(encoding="utf-8")
    assert "gdelt" not in source.lower()
    assert "NewsMacroBrief" not in source
    assert "situation_brief_store" not in source
    assert "run_cycle" not in source or "fetch_in_run_cycle" in source
    assert "RegisterWorldCohort" not in source
    assert "StartWorldCohort" not in source
    assert "ArmWorldCohort" not in source


def test_collection_plan_is_source_backed_deterministic_and_excludes_unsourced_venues() -> None:
    from trader.domain.world_macro import MacroCollectionPlan
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs
    from trader.runtime.world_macro_runtime import collection_plan, collection_scopes

    bundle = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    plan = collection_plan(bundle)
    scopes = collection_scopes(bundle)
    expected = MacroCollectionPlan.from_registry(bundle.registry)
    assert plan == expected
    from trader.domain.world_macro import WORLD_MACRO_COLLECTION_PLAN_ID, WORLD_MACRO_COLLECTION_PLAN_SHA256

    assert plan.plan_id == WORLD_MACRO_COLLECTION_PLAN_ID
    assert plan.content_sha256 == WORLD_MACRO_COLLECTION_PLAN_SHA256
    assert plan.registry_content_sha256 == bundle.registry.content_sha256
    assert scopes == expected.scopes
    assert collection_scopes(bundle) == scopes
    ids = {(scope.kind, scope.entity_id) for scope in scopes}
    kinds = {scope.kind for scope in scopes}
    assert kinds == {"world", "region", "country"}
    assert ids == {
        ("country", "iso-3166:US"),
        ("region", "iso-un-m49:150"),
        ("world", "market"),
    }
    assert ("venue", "mic:XTAI") not in ids
    assert not any(scope.kind == "venue" for scope in scopes)
    by_scope = {(target.scope.kind, target.scope.entity_id): target.source_ids for target in plan.targets}
    assert by_scope[("country", "iso-3166:US")] == ("cpi_us_imf", "fed_funds_effective", "unemployment_rate_us")
    assert by_scope[("region", "iso-un-m49:150")] == ("ecb_deposit_rate", "hicp_euro_area")
    assert by_scope[("world", "market")] == ("brent_crude_usd", "gold_usd")
    assert sum(len(target.source_ids) for target in plan.targets) == 7
    mapping_ids = {
        (getattr(ref, "kind"), getattr(ref, "entity_id"))
        for entry in bundle.scope_mapping.entries
        for ref in (entry.venue, entry.country, entry.region, entry.world)
    }
    assert ids < mapping_ids
    inspect_source = inspect.getsource(collection_plan)
    assert "committed_macro_collection_plan" in inspect_source
    assert "control_scopes" not in inspect_source
    assert "WORLD_MARKET_CONTROL_SCOPE" not in inspect_source
    assert "scope_mapping" not in inspect.getsource(collection_scopes)
    assert "mapping_entry" not in inspect.getsource(collection_scopes)


def test_collection_plan_fails_closed_when_registry_drifts_from_committed_identity() -> None:
    from trader.domain.world_macro import MacroSourceRegistry, MacroSourceRegistryEntry, committed_macro_collection_plan
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs
    from trader.runtime.world_macro_runtime import collection_plan

    bundle = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    extra = MacroSourceRegistryEntry(
        source_id="drifted_extra",
        provider_id="dbnomics",
        provider_entity_id="DRIFT/EXTRA",
        canonical_scope=MacroScope(kind="world", entity_id="market"),
        adapter_version="world_dbnomics_series.v1",
        fact_kind="series_point",
        metric_key="policy_rate",
    )
    drifted = MacroSourceRegistry(
        registry_version=bundle.registry.registry_version,
        entries=(*bundle.registry.entries, extra),
    )
    with pytest.raises(ValueError, match="committed identity"):
        committed_macro_collection_plan(drifted)

    class _Drifted:
        registry = drifted

    with pytest.raises(ValueError, match="committed identity"):
        collection_plan(_Drifted())


def test_live_bridge_lineage_binds_derived_ontology_and_collection_plan() -> None:
    from trader.application.world_model.ontology_bootstrap import derive_market_ontology
    from trader.domain.world_graph_bridge_lifecycle import (
        committed_macro_graph_bridge_spec,
        require_committed_live_bridge_lineage,
    )
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs
    from trader.runtime.world_macro_runtime import collection_plan

    bundle = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    plan = collection_plan(bundle)
    _, _, revision = derive_market_ontology(bundle.scope_mapping)
    spec = require_committed_live_bridge_lineage(
        mapping=bundle.scope_mapping,
        ontology=revision,
        collection_plan=plan,
    )
    assert spec == committed_macro_graph_bridge_spec()


def test_scope_mapping_covers_live_anchors_and_graph_bootstrap_uses_full_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.application.test_world_scope_universe_coverage import COMMITTED_UNIVERSE_ANCHORS
    from trader.application.world_model.ontology_bootstrap import derive_market_ontology
    from trader.domain.world_scope import WorldMarketAnchorRef
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs
    from trader.runtime.world_macro_runtime import collection_scopes, wire_world_macro_runtime

    operator = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    mapping = operator.scope_mapping
    assert mapping.mapping_id == "world_scope_mapping.v1"
    assert len(COMMITTED_UNIVERSE_ANCHORS) == 33
    for market_venue, instrument in COMMITTED_UNIVERSE_ANCHORS:
        resolved = mapping.resolve(WorldMarketAnchorRef(market_venue=market_venue, instrument=instrument))
        assert resolved.status == "resolved", (market_venue, instrument)
    historical = (("TW", "2301.TW"), ("TW", "2404.TW"), ("TW", "6488.TWO"))
    for market_venue, instrument in historical:
        resolved = mapping.resolve(WorldMarketAnchorRef(market_venue=market_venue, instrument=instrument))
        assert resolved.status == "resolved", (market_venue, instrument)
    assert len(mapping.entries) == 36

    collection_ids = {(scope.kind, scope.entity_id) for scope in collection_scopes(operator)}
    assert collection_ids == {
        ("country", "iso-3166:US"),
        ("region", "iso-un-m49:150"),
        ("world", "market"),
    }
    entities, _relations, revision = derive_market_ontology(mapping)
    venue_ids = {entity.entity_id for entity in entities if entity.kind == "venue"}
    assert "mic:XTAI" in venue_ids
    assert "mic:XNYS" in venue_ids
    assert ("venue", "mic:XTAI") not in collection_ids
    assert revision.scope_mapping_id == mapping.mapping_id
    assert revision.scope_mapping_hash == mapping.content_sha256

    captured: list[object] = []

    class _Bootstrap:
        def __init__(self, _store: object, mapping_arg: object) -> None:
            captured.append(mapping_arg)

        def ensure_published(self, **_kwargs: object) -> object:
            return SimpleNamespace(status="ready")

        def expected_revision(self) -> object:
            return revision

    monkeypatch.setattr(
        "trader.application.world_model.ontology_bootstrap.WorldOntologyBootstrapService",
        _Bootstrap,
    )
    monkeypatch.setattr(
        "trader.application.world_model.graph_observation_bridge.RegisterMacroObservationKnowledge",
        lambda **_kwargs: SimpleNamespace(
            ensure=lambda _request_id: SimpleNamespace(events=(), active_run=None),
            reconcile=lambda **_k: SimpleNamespace(events=(), active_run=None),
        ),
    )
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=UrlFixtureTransport(),
        clock=FakeClock(NOW),
        sleeper=lambda _seconds: None,
        graph_enabled=True,
    )
    first = bundle.runner.trigger(now=NOW, reason="full-mapping")
    assert first["triggered"] is True
    first["_thread"].join(timeout=15.0)
    assert first["_thread"].is_alive() is False
    bundle.runner.stop()
    assert captured
    assert captured[0] is bundle.mapping
    assert len(captured[0].entries) == 36
    assert any(entry.venue.entity_id == "mic:XTAI" for entry in captured[0].entries)


def test_wire_does_not_fetch_until_trigger(tmp_path: Path) -> None:
    from trader.runtime.world_macro_runtime import MACRO_LANE_IDENTITY, wire_world_macro_runtime

    transport = ScriptedTransport([])
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=transport,
        clock=FakeClock(NOW),
        sleeper=lambda _seconds: None,
    )
    assert transport.calls == []
    assert bundle.lane_identity == MACRO_LANE_IDENTITY
    assert bundle.budgets.retry_max == 1
    assert bundle.budgets.honor_retry_after is True
    assert bundle.budgets.fetch_in_run_cycle is False
    assert bundle.budgets.fetch_in_world_capture_worker is False
    assert bundle.budgets.worker == "single_flight"
    assert all(provider.cooldown_h == 24 for provider in bundle.budgets.providers.values())
    assert bundle.runner.thread_name != "world-model-shadow"


class UrlFixtureTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> object:
        from trader.infrastructure.market_sources.world_macro.series import MacroHttpResponse

        del timeout_s, headers
        with self.lock:
            self.calls.append(url)
        if "finance.yahoo.com" in url:
            body = _yahoo_body("2026-08-21", 91.22)
        else:
            body = _dbnomics_body("2026-08-22", 4.33)
        return MacroHttpResponse(status=200, body=body, headers={})


def test_wired_worker_honors_adapter_24h_cooldown_without_extra_http(tmp_path: Path) -> None:
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs
    from trader.runtime.world_macro_runtime import (
        MACRO_THREAD_NAME,
        collection_plan,
        collection_scopes,
        wire_world_macro_runtime,
    )

    operator = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    plan = collection_plan(operator)
    assert len(collection_scopes(operator)) == 3
    assert len(plan.targets) == 3
    assert sum(len(target.source_ids) for target in plan.targets) == 7
    clock = FakeClock(NOW)
    transport = UrlFixtureTransport()
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=transport,
        clock=clock,
        sleeper=lambda _seconds: None,
    )
    first = bundle.runner.trigger(now=NOW, reason="first")
    assert first["triggered"] is True
    first["_thread"].join(timeout=15.0)
    assert first["_thread"].is_alive() is False
    first_calls = list(transport.calls)
    assert len(first_calls) == 7
    assert len(set(first_calls)) == 7
    events = _jsonl_payloads(tmp_path / "world_macro" / "runs" / "events")
    registered = [row for row in events if row.get("event_type") == "macro_collection_registered"]
    source_results = [
        row for row in events if row.get("event_type") in {"macro_source_completed", "macro_source_failed"}
    ]
    assert len(registered) == 3
    assert {tuple(row["expected_source_ids"]) for row in registered} == {
        ("cpi_us_imf", "fed_funds_effective", "unemployment_rate_us"),
        ("ecb_deposit_rate", "hicp_euro_area"),
        ("brent_crude_usd", "gold_usd"),
    }
    assert len(source_results) == 7
    clock.advance(hours=1)
    second = bundle.runner.trigger(now=clock(), reason="replay")
    assert second["triggered"] is True
    second["_thread"].join(timeout=5.0)
    assert second["_thread"].is_alive() is False
    assert transport.calls == first_calls
    replay_events = _jsonl_payloads(tmp_path / "world_macro" / "runs" / "events")
    replay_results = [
        row for row in replay_events if row.get("event_type") in {"macro_source_completed", "macro_source_failed"}
    ]
    assert len(replay_results) == 14
    bundle.runner.stop()
    status = bundle.runner.status()
    assert status["status"] in {"ok", "partial"}
    assert status.get("lane_identity") == "world.context.macro"
    assert not any(thread.name == MACRO_THREAD_NAME and thread.is_alive() for thread in threading.enumerate())


class MutableMacroTransport:
    def __init__(self, *, db_period: str, db_value: float, indexed_at: str) -> None:
        self.db_period = db_period
        self.db_value = db_value
        self.indexed_at = indexed_at
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> object:
        from trader.infrastructure.market_sources.world_macro.series import MacroHttpResponse

        del timeout_s, headers
        with self.lock:
            self.calls.append(url)
        if "finance.yahoo.com" in url:
            return MacroHttpResponse(status=200, body=_yahoo_body("2026-08-21", 91.22), headers={})
        body = json.dumps(
            {
                "series": {
                    "docs": [
                        {
                            "period": [self.db_period],
                            "value": [self.db_value],
                            "indexed_at": self.indexed_at,
                        }
                    ]
                }
            }
        )
        return MacroHttpResponse(status=200, body=body, headers={})


def _ecb_facts(root: Path, clock: FakeClock) -> list:
    from trader.infrastructure.state_db.world_macro_store import WorldMacroStore

    prefix = "ECB/FM/B.U2.EUR.4F.KR.DFR.LEV:"
    return [
        fact
        for fact in WorldMacroStore(root / "world_macro", clock=clock).list_facts()
        if fact.source.source_record_id.startswith(prefix)
    ]


def test_wired_restart_replays_correction_leaf_then_extends_chain(tmp_path: Path) -> None:
    from trader.runtime.world_macro_runtime import wire_world_macro_runtime

    clock = FakeClock(NOW)
    transport = MutableMacroTransport(db_period="2026-08", db_value=4.25, indexed_at="2026-08-20T12:00:00Z")
    live = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=transport,
        clock=clock,
        sleeper=lambda _seconds: None,
    )
    started_a = live.runner.trigger(now=clock(), reason="value-a")
    assert started_a["triggered"] is True
    started_a["_thread"].join(timeout=15.0)
    clock.advance(hours=25)
    transport.db_value = 4.50
    transport.indexed_at = "2026-08-23T12:00:00Z"
    started_b = live.runner.trigger(now=clock(), reason="value-b")
    assert started_b["triggered"] is True
    started_b["_thread"].join(timeout=15.0)
    live.runner.stop()

    ecb = _ecb_facts(tmp_path, clock)
    assert len(ecb) == 2
    value_a = next(fact for fact in ecb if fact.supersedes_fact_version_id is None)
    value_b = next(fact for fact in ecb if fact.supersedes_fact_version_id is not None)
    assert value_a.value.number == 4.25
    assert value_b.value.number == 4.50
    assert value_b.supersedes_fact_version_id == value_a.fact_version_id.value

    clock.advance(hours=1)
    rebuilt = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=transport,
        clock=clock,
        sleeper=lambda _seconds: None,
    )
    started_replay = rebuilt.runner.trigger(now=clock(), reason="replay-b")
    assert started_replay["triggered"] is True
    started_replay["_thread"].join(timeout=15.0)
    rebuilt.runner.stop()
    replayed = _ecb_facts(tmp_path, clock)
    assert len(replayed) == 2
    replayed_b = next(fact for fact in replayed if fact.fact_version_id == value_b.fact_version_id)
    assert replayed_b.supersedes_fact_version_id == value_a.fact_version_id.value
    assert replayed_b.content_sha256 == value_b.content_sha256

    clock.advance(hours=25)
    transport.db_value = 4.75
    transport.indexed_at = "2026-08-24T12:00:00Z"
    extended = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=transport,
        clock=clock,
        sleeper=lambda _seconds: None,
    )
    started_c = extended.runner.trigger(now=clock(), reason="value-c")
    assert started_c["triggered"] is True
    started_c["_thread"].join(timeout=15.0)
    extended.runner.stop()
    final_ecb = _ecb_facts(tmp_path, clock)
    assert len(final_ecb) == 3
    value_c = next(fact for fact in final_ecb if fact.value.number == 4.75)
    assert value_c.supersedes_fact_version_id == value_b.fact_version_id.value


def test_graph_flag_defaults_off_and_collect_does_not_create_graph_relations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.runtime.world_macro_runtime import collect_world_macro, wire_world_macro_runtime

    monkeypatch.delenv("CASYS_WORLD_MODEL_GRAPH_ENABLED", raising=False)
    collect_source = inspect.getsource(collect_world_macro)
    assert "RegisterMacroObservationKnowledge" not in collect_source
    assert "OBSERVES" not in collect_source
    runtime_source = (REPO_ROOT / "trader" / "runtime" / "world_macro_runtime.py").read_text(encoding="utf-8")
    assert "CASYS_WORLD_MODEL_GRAPH_ENABLED" in runtime_source
    assert 'env.get(GRAPH_FLAG, "0")' in runtime_source
    from trader.runtime.world_macro_runtime import graph_enabled

    assert graph_enabled({}) is False
    assert graph_enabled({"CASYS_WORLD_MODEL_GRAPH_ENABLED": "0"}) is False
    assert graph_enabled({"CASYS_WORLD_MODEL_GRAPH_ENABLED": "1"}) is True
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=UrlFixtureTransport(),
        clock=FakeClock(NOW),
        sleeper=lambda _seconds: None,
    )
    first = bundle.runner.trigger(now=NOW, reason="first")
    assert first["triggered"] is True
    first["_thread"].join(timeout=60.0)
    bundle.runner.stop()
    world_model_db = tmp_path / "world_model.db"
    if world_model_db.exists():
        from trader.infrastructure.state_db.world_graph_store import WorldGraphStore

        graph = WorldGraphStore(world_model_db, clock=lambda: NOW)
        try:
            assert graph.list_knowledge_relation_events_available_through(NOW) == ()
            assert graph.load("macro_graph_bridge.v1").events == ()
        finally:
            graph.close()


def _stub_ontology_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    from trader.application.world_model.ontology_bootstrap import derive_market_ontology
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs

    revision = derive_market_ontology(load_world_macro_operator_configs(config_dir=CONFIG_DIR).scope_mapping)[2]
    monkeypatch.setattr(
        "trader.application.world_model.ontology_bootstrap.WorldOntologyBootstrapService",
        lambda *_args, **_kwargs: SimpleNamespace(
            ensure_published=lambda **_k: SimpleNamespace(status="ready"),
            expected_revision=lambda: revision,
        ),
    )


def test_graph_enabled_runs_in_macro_worker_not_episode_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.runtime.world_macro_runtime import collect_world_macro, wire_world_macro_runtime

    monkeypatch.setenv("CASYS_WORLD_MODEL_GRAPH_ENABLED", "1")
    _stub_ontology_bootstrap(monkeypatch)
    calls: list[str] = []

    class _Bridge:
        def align(self, request_id: str) -> object:
            return self.ensure(request_id)

        def ensure(self, request_id: str) -> object:
            calls.append(f"ensure:{request_id}")
            return SimpleNamespace(events=(), active_run=None)

        def reconcile(self, *, limit: int) -> object:
            calls.append(f"reconcile:{limit}")
            return SimpleNamespace(events=())

        def activate(self, request_id: str) -> object:
            calls.append(f"activate:{request_id}")
            return SimpleNamespace(events=(), active_run=None)

    monkeypatch.setattr(
        "trader.application.world_model.graph_observation_bridge.RegisterMacroObservationKnowledge",
        lambda **_kwargs: _Bridge(),
    )
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=UrlFixtureTransport(),
        clock=FakeClock(NOW),
        sleeper=lambda _seconds: None,
        graph_enabled=True,
    )
    first = bundle.runner.trigger(now=NOW, reason="first")
    assert first["triggered"] is True
    first["_thread"].join(timeout=60.0)
    bundle.runner.stop()
    assert any(item.startswith("ensure:") for item in calls)
    assert any(item.startswith("reconcile:") for item in calls)
    assert not any(item.startswith("activate:") for item in calls)
    assert "RegisterMacroObservationKnowledge" not in inspect.getsource(collect_world_macro)


def _jsonl_payloads(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if not root.exists():
        return rows
    for path in root.rglob("*.jsonl"):
        if "availability_receipts" in path.parts:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            payload = json.loads(text)
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


class _FakeHttpResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self.status = status
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_default_macro_runtime_with_mocked_urlopen_yields_typed_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.error

    from trader.domain.world_macro import MacroSourceFact, MacroWorldObservation
    from trader.runtime.world_macro_runtime import wire_world_macro_runtime

    failing = {"value": True}

    def fake_urlopen(req: object, timeout: object = None) -> object:
        del timeout
        url = str(getattr(req, "full_url", req))
        if failing["value"]:
            raise urllib.error.URLError("network down")
        if "finance.yahoo.com" in url:
            return _FakeHttpResponse(_yahoo_body("2026-08-21", 91.22))
        return _FakeHttpResponse(_dbnomics_body("2026-08-22", 4.33))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    wire_source = inspect.getsource(wire_world_macro_runtime)
    assert "UrllibMacroTransport()" in wire_source
    assert "transport or urllib_macro_transport" not in wire_source
    assert "graph_enabled()" not in wire_source

    clock = FakeClock(NOW)
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        clock=clock,
        sleeper=lambda _seconds: None,
    )
    first = bundle.runner.trigger(now=NOW, reason="historical-failure")
    assert first["triggered"] is True
    first["_thread"].join(timeout=60.0)
    failed_events = [
        row
        for row in _jsonl_payloads(tmp_path / "world_macro" / "runs" / "events")
        if row.get("event_type") == "macro_source_failed"
    ]
    assert failed_events
    failed_count = len(failed_events)
    assert _jsonl_payloads(tmp_path / "world_macro" / "facts") == []

    failing["value"] = False
    clock.advance(hours=1)
    second = bundle.runner.trigger(now=clock(), reason="proven-run")
    assert second["triggered"] is True
    second["_thread"].join(timeout=60.0)
    bundle.runner.stop()

    facts = [
        MacroSourceFact.from_mapping(row)
        for row in _jsonl_payloads(tmp_path / "world_macro" / "facts")
        if row.get("fact_version_id")
    ]
    observations = [
        MacroWorldObservation.from_mapping(row)
        for row in _jsonl_payloads(tmp_path / "world_macro" / "observations")
        if row.get("producer_version") and row.get("observation_id")
    ]
    assert facts
    assert observations
    assert all(item.fact_version_id.value.startswith("macro_source_fact_version:v1:") for item in facts)
    assert all(item.observation_id.startswith("macro_world_observation:v1:") for item in observations)
    remaining_failures = [
        row
        for row in _jsonl_payloads(tmp_path / "world_macro" / "runs" / "events")
        if row.get("event_type") == "macro_source_failed"
    ]
    assert len(remaining_failures) == failed_count


def test_graph_yaml_only_enables_macro_graph_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trader.runtime.world_macro_runtime import wire_world_macro_runtime

    monkeypatch.delenv("CASYS_WORLD_MODEL_GRAPH_ENABLED", raising=False)
    _stub_ontology_bootstrap(monkeypatch)
    calls: list[str] = []

    class _Bridge:
        def align(self, request_id: str) -> object:
            return self.ensure(request_id)

        def ensure(self, request_id: str) -> object:
            calls.append(f"ensure:{request_id}")
            return SimpleNamespace(events=(), active_run=None)

        def reconcile(self, *, limit: int) -> object:
            calls.append(f"reconcile:{limit}")
            return SimpleNamespace(events=())

        def activate(self, request_id: str) -> object:
            calls.append(f"activate:{request_id}")
            return SimpleNamespace(events=(), active_run=None)

    monkeypatch.setattr(
        "trader.application.world_model.graph_observation_bridge.RegisterMacroObservationKnowledge",
        lambda **_kwargs: _Bridge(),
    )
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=UrlFixtureTransport(),
        clock=FakeClock(NOW),
        sleeper=lambda _seconds: None,
        graph_enabled=True,
    )
    first = bundle.runner.trigger(now=NOW, reason="yaml-only")
    assert first["triggered"] is True
    first["_thread"].join(timeout=60.0)
    bundle.runner.stop()
    assert any(item.startswith("ensure:") for item in calls)
    assert any(item.startswith("reconcile:") for item in calls)
    assert not any(item.startswith("activate:") for item in calls)


def test_graph_bridge_ensure_then_reconcile_never_activates_every_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.runtime.world_macro_runtime import wire_world_macro_runtime

    _stub_ontology_bootstrap(monkeypatch)
    captured: list[dict[str, object]] = []
    calls: list[str] = []

    class _Bridge:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

        def align(self, request_id: str) -> object:
            return self.ensure(request_id)

        def ensure(self, request_id: str) -> object:
            calls.append(f"ensure:{request_id}")
            return SimpleNamespace(
                events=(),
                active_run=SimpleNamespace(status="active", block_reason=None),
                version=1,
            )

        def reconcile(self, *, limit: int) -> object:
            calls.append(f"reconcile:{limit}")
            return SimpleNamespace(
                events=(),
                active_run=SimpleNamespace(status="active", block_reason=None),
                version=1,
            )

        def activate(self, request_id: str) -> object:
            calls.append(f"activate:{request_id}")
            return SimpleNamespace(events=(), active_run=None)

        def handoff(self, **_kwargs: object) -> object:
            calls.append("handoff")
            return SimpleNamespace(events=(), active_run=None, version=1)

    monkeypatch.setattr(
        "trader.application.world_model.graph_observation_bridge.RegisterMacroObservationKnowledge",
        lambda **kwargs: _Bridge(**kwargs),
    )
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=UrlFixtureTransport(),
        clock=FakeClock(NOW),
        sleeper=lambda _seconds: None,
        graph_enabled=True,
    )
    first = bundle.runner.trigger(now=NOW, reason="align")
    assert first["triggered"] is True
    first["_thread"].join(timeout=60.0)
    bundle.runner.stop()
    assert any(item.startswith("ensure:") for item in calls)
    assert calls.count("reconcile:32") == 1
    assert "handoff" not in calls
    assert not any(item.startswith("activate:") for item in calls)
    assert captured
    assert captured[0]["collection_plan"].__class__.__name__ == "MacroCollectionPlan"
    runtime_source = (REPO_ROOT / "trader" / "runtime" / "world_macro_runtime.py").read_text(encoding="utf-8")
    assert "use_case.align(" in runtime_source
    assert "use_case.activate(" not in runtime_source
    assert "collection_plan=collection_plan" in runtime_source


def test_bridge_lifecycle_aligns_before_collection_even_when_every_provider_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.runtime.world_macro_runtime import wire_world_macro_runtime

    _stub_ontology_bootstrap(monkeypatch)
    calls: list[str] = []

    class _Bridge:
        def align(self, request_id: str) -> object:
            return self.ensure(request_id)

        def ensure(self, request_id: str) -> object:
            calls.append("ensure")
            return SimpleNamespace(
                events=(),
                active_run=SimpleNamespace(status="active", block_reason=None),
                version=2,
            )

        def reconcile(self, *, limit: int) -> object:
            calls.append(f"reconcile:{limit}")
            return SimpleNamespace(
                events=(),
                active_run=SimpleNamespace(status="active", block_reason=None),
                version=2,
            )

        def activate(self, request_id: str) -> object:
            calls.append(f"activate:{request_id}")
            return SimpleNamespace(events=(), active_run=None)

    monkeypatch.setattr(
        "trader.application.world_model.graph_observation_bridge.RegisterMacroObservationKnowledge",
        lambda **_kwargs: _Bridge(),
    )

    class FailAll:
        def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> object:
            del timeout_s, headers
            calls.append(f"http:{url}")
            raise ValueError("invalid series payload")

    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=FailAll(),
        clock=FakeClock(NOW),
        sleeper=lambda _seconds: None,
        graph_enabled=True,
    )
    first = bundle.runner.trigger(now=NOW, reason="post_cycle")
    assert first["triggered"] is True
    first["_thread"].join(timeout=15.0)
    assert first["_thread"].is_alive() is False
    assert calls[0] == "ensure"
    http_calls = [item for item in calls if item.startswith("http:")]
    assert len(http_calls) == 7
    assert "reconcile:32" in calls
    events = _jsonl_payloads(tmp_path / "world_macro" / "runs" / "events")
    registered = [row for row in events if row.get("event_type") == "macro_collection_registered"]
    source_results = [
        row for row in events if row.get("event_type") in {"macro_source_completed", "macro_source_failed"}
    ]
    assert len(registered) == 3
    assert len(source_results) == 7
    assert {row.get("reason") for row in source_results if row.get("event_type") == "macro_source_failed"} <= {
        "invalid_payload",
        "unavailable",
        "timeout",
    }
    replay = bundle.runner.trigger(now=NOW, reason="replay")
    assert replay["triggered"] is True
    replay["_thread"].join(timeout=15.0)
    bundle.runner.stop()
    replay_events = _jsonl_payloads(tmp_path / "world_macro" / "runs" / "events")
    replay_registered = [row for row in replay_events if row.get("event_type") == "macro_collection_registered"]
    replay_results = [
        row for row in replay_events if row.get("event_type") in {"macro_source_completed", "macro_source_failed"}
    ]
    assert len(replay_registered) == 3
    assert len(replay_results) == 7
    status = bundle.runner.status()
    assert status["status"] in {"partial", "failed"}
    runtime_source = (REPO_ROOT / "trader" / "runtime" / "world_macro_runtime.py").read_text(encoding="utf-8")
    start = runtime_source.index("def collect_fn")
    body = runtime_source[start : runtime_source.index("runner = WorldMacroBackgroundRunner", start)]
    assert "_align_macro_graph_bridge(" in body
    assert body.index("_align_macro_graph_bridge(") < body.index("collect_world_macro(")
    assert body.index("collect_world_macro(") < body.index("_reconcile_macro_graph_bridge(")


def test_typed_graph_flag_false_does_not_reread_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trader.runtime.world_macro_runtime import MACRO_THREAD_NAME, wire_world_macro_runtime

    monkeypatch.setenv("CASYS_WORLD_MODEL_GRAPH_ENABLED", "1")
    calls: list[str] = []
    monkeypatch.setattr(
        "trader.application.world_model.graph_observation_bridge.RegisterMacroObservationKnowledge",
        lambda **_kwargs: calls.append("bridge") or SimpleNamespace(),
    )
    live_before = {
        thread.ident for thread in threading.enumerate() if thread.name == MACRO_THREAD_NAME and thread.is_alive()
    }
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=UrlFixtureTransport(),
        clock=FakeClock(NOW),
        sleeper=lambda _seconds: None,
        graph_enabled=False,
    )
    first = bundle.runner.trigger(now=NOW, reason="env-ignored")
    assert first["triggered"] is True
    first["_thread"].join(timeout=15.0)
    assert first["_thread"].is_alive() is False
    bundle.runner.stop()
    assert calls == []
    world_model_db = tmp_path / "world_model.db"
    assert not world_model_db.exists()
    live_after = {
        thread
        for thread in threading.enumerate()
        if thread.name == MACRO_THREAD_NAME and thread.is_alive() and thread.ident not in live_before
    }
    assert live_after == set()
