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
from trader.domain.world_macro import MacroScope


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


def test_macro_lane_identity_is_distinct_from_v1_and_context_v2() -> None:
    from trader.runtime.world_macro_runtime import MACRO_LANE_IDENTITY, MACRO_THREAD_NAME

    assert MACRO_LANE_IDENTITY == "context.v2.macro_source.v1"
    assert MACRO_LANE_IDENTITY != "context.v2"
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


def test_collect_runs_each_scope_once_sequentially_without_worker_retries() -> None:
    from trader.runtime.world_macro_runtime import collect_world_macro

    scopes = (
        MacroScope(kind="world", entity_id="market"),
        MacroScope(kind="venue", entity_id="mic:XTAI"),
    )
    calls: list[MacroScope] = []

    class Pipeline:
        def collect(self, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs["scope"])  # type: ignore[arg-type]
            assert kwargs["cutoff_at"] == NOW
            return SimpleNamespace(status="completed", run_id=f"run:{kwargs['scope']}")

    report = collect_world_macro(
        now=NOW,
        reason="post_cycle",
        pipeline=Pipeline(),  # type: ignore[arg-type]
        registry=SimpleNamespace(entries=()),
        sources={"fed_funds_effective": object()},
        scopes=scopes,
        budgets=SimpleNamespace(
            fetch_in_run_cycle=False,
            fetch_in_world_capture_worker=False,
            max_parallel_requests=1,
            retry_max=1,
            honor_retry_after=True,
        ),
    )
    assert calls == list(scopes)
    assert report["status"] == "ok"
    assert report["lane_identity"] == "context.v2.macro_source.v1"
    assert report["authority"] == "shadow_only"
    assert report["decision_effect"] == "none"
    assert len(report["runs"]) == 2
    source = inspect.getsource(collect_world_macro)
    assert "retry" not in source.lower()
    assert "ThreadPool" not in source
    assert "concurrent" not in source


def test_scope_failure_does_not_abort_remaining_scopes_or_raise() -> None:
    from trader.runtime.world_macro_runtime import collect_world_macro

    scopes = (
        MacroScope(kind="country", entity_id="iso-3166:US"),
        MacroScope(kind="world", entity_id="market"),
    )

    class Pipeline:
        def collect(self, **kwargs: object) -> SimpleNamespace:
            scope = kwargs["scope"]
            assert isinstance(scope, MacroScope)
            if scope.kind == "country":
                raise RuntimeError("http 429 too many requests")
            return SimpleNamespace(status="completed", run_id="run-world")

    report = collect_world_macro(
        now=NOW,
        reason="post_cycle",
        pipeline=Pipeline(),  # type: ignore[arg-type]
        registry=object(),
        sources={},
        scopes=scopes,
    )
    assert report["status"] == "partial"
    assert len(report["errors"]) == 1
    assert report["runs"][0]["status"] == "completed"
    assert report["runs"][0]["scope"]["kind"] == "world"


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


def test_collection_scopes_are_canonical_and_include_mapping_venues() -> None:
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs
    from trader.runtime.world_macro_runtime import collection_scopes

    bundle = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    scopes = collection_scopes(bundle)
    kinds = {scope.kind for scope in scopes}
    ids = {(scope.kind, scope.entity_id) for scope in scopes}
    assert kinds == {"world", "region", "country", "venue"}
    assert ("world", "market") in ids
    assert ("venue", "mic:XTAI") in ids
    assert ("country", "iso-3166:US") in ids
    assert scopes == tuple(sorted(scopes, key=lambda item: (item.kind, item.entity_id)))


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
    from trader.runtime.world_macro_runtime import wire_world_macro_runtime

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
    first["_thread"].join(timeout=60.0)
    assert first["_thread"].is_alive() is False
    first_calls = list(transport.calls)
    assert len(first_calls) == 7
    assert len(set(first_calls)) == 7
    clock.advance(hours=1)
    second = bundle.runner.trigger(now=clock(), reason="replay")
    assert second["triggered"] is True
    second["_thread"].join(timeout=60.0)
    assert second["_thread"].is_alive() is False
    assert transport.calls == first_calls
    bundle.runner.stop()
    status = bundle.runner.status()
    assert status["status"] in {"ok", "partial"}
    assert status.get("lane_identity") == "context.v2.macro_source.v1"
