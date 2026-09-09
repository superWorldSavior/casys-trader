"""Numeric regime derivation and timestamp clocks on the real macro path.

Covers adapter → admission → projector → store, plus the wired runtime
composer. Fixtures are small and representative; live DBnomics is not called.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MACRO_SOURCE_REGISTRY_VERSION,
    MACRO_TRANSFORM_VERSION,
    MacroCollectionPlan,
    MacroDerivationPolicy,
    MacroNumericValue,
    MacroScope,
    MacroSourceFact,
    MacroSourceRegistry,
)
from trader.infrastructure.state_db.world_macro_store import WorldMacroStore


UTC = timezone.utc
CONFIG_DIR = REPO_ROOT / "config"
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
RUN_SCOPE = MacroScope(kind="venue", entity_id="mic:XTAI")
US_SCOPE = MacroScope(kind="country", entity_id="iso-3166:US")
EU_SCOPE = MacroScope(kind="region", entity_id="iso-un-m49:150")


@pytest.fixture(autouse=True)
def _forbid_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("live network is forbidden in numeric derivation tests")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


class ScriptedTransport:
    def __init__(self, script: list[object] | None = None, *, by_url: dict[str, str] | None = None) -> None:
        self.script = list(script or ())
        self.by_url = dict(by_url or {})
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> object:
        from trader.infrastructure.market_sources.world_macro.series import MacroHttpResponse

        del timeout_s, headers
        with self.lock:
            self.calls.append(url)
            if url in self.by_url:
                return MacroHttpResponse(status=200, body=self.by_url[url], headers={})
            if not self.script:
                raise AssertionError(f"unexpected extra HTTP call: {url}")
            item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return MacroHttpResponse(status=200, body=str(item), headers={})


def _dbnomics_body(
    *points: tuple[str, float],
    indexed_at: str | None = "2025-01-01T00:00:00Z",
) -> str:
    if not points:
        raise AssertionError("fixture requires at least one period/value")
    periods = [item[0] for item in points]
    values = [item[1] for item in points]
    doc: dict[str, object] = {"period": periods, "value": values}
    if indexed_at is not None:
        doc["indexed_at"] = indexed_at
    return json.dumps({"series": {"docs": [doc]}})


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


def _bundle():
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs

    return load_world_macro_operator_configs(config_dir=CONFIG_DIR)


def _committed_policy() -> MacroDerivationPolicy:
    return _bundle().policy


def _ports(transport: ScriptedTransport, *, clock: FakeClock | None = None, leaves=None):
    from trader.infrastructure.market_sources.world_macro import build_macro_source_ports

    return build_macro_source_ports(
        _bundle(),
        transport=transport,
        clock=clock or FakeClock(CUTOFF),
        sleeper=lambda _seconds: None,
        leaves=leaves,
    )


def _us_registry() -> MacroSourceRegistry:
    bundle = _bundle()
    return MacroSourceRegistry(
        registry_version=bundle.registry.registry_version,
        entries=(bundle.registry.entry_for("fed_funds_effective"),),
    )


def _collect_us(*, points: tuple[tuple[str, float], ...], cutoff: datetime = CUTOFF, clock: FakeClock | None = None, tmp_path: Path):
    from trader.application.world_model.macro_pipeline import MacroWorldPipeline
    from trader.infrastructure.market_sources.world_macro import build_macro_source_ports

    bundle = _bundle()
    resolved_clock = clock or FakeClock(cutoff)
    store = WorldMacroStore(tmp_path, clock=resolved_clock)
    transport = ScriptedTransport([_dbnomics_body(*points, indexed_at="2025-01-01T00:00:00Z")])
    ports = build_macro_source_ports(
        bundle,
        transport=transport,
        clock=resolved_clock,
        sleeper=lambda _seconds: None,
    )
    registry = _us_registry()
    target = MacroCollectionPlan.from_registry(registry).target_for(US_SCOPE)
    pipeline = MacroWorldPipeline(history=store, ledger=store, reader=store, policy=bundle.policy)
    run = pipeline.collect(
        target=target,
        cutoff_at=cutoff,
        registry=registry,
        sources={"fed_funds_effective": ports["fed_funds_effective"]},
        observed_at=cutoff,
    )
    return run, store, ports["fed_funds_effective"]


def test_committed_policy_parses_rate_thresholds_and_unknown_methods() -> None:
    raw = yaml.safe_load((CONFIG_DIR / "world_macro_derivation_policy.yaml").read_text(encoding="utf-8"))
    policy = _committed_policy()
    rates = policy.rule_for("rates_regime")
    usd = policy.rule_for("usd_regime")
    macro = policy.rule_for("macro_regime")
    assert policy.transform_version == MACRO_TRANSFORM_VERSION
    assert policy.producer_version == MACRO_PRODUCER_VERSION
    assert policy.content_sha256 == raw["content_sha256"]
    assert rates is not None
    assert rates.method == "last_two_admissible_points_delta_pp"
    assert rates.metric_keys == ("policy_rate",)
    assert rates.rising_if_delta_pp_ge == 0.125
    assert rates.falling_if_delta_pp_le == -0.125
    assert rates.stable_if_abs_delta_pp_lt == 0.125
    assert rates.on_single_point == "unknown"
    assert rates.on_conflict == "unknown"
    assert usd is not None
    assert usd.method == "always_unknown"
    assert usd.value == "unknown"
    assert "broad_usd_index" in usd.missing_source_ids
    assert macro is not None
    assert macro.method == "always_unknown_until_inflation_yoy_proven"
    assert macro.do_not_infer_yoy_from_single_index_level is True
    assert macro.do_not_use_front_month_futures is True


def test_policy_content_hash_changes_when_rate_threshold_changes(tmp_path: Path) -> None:
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs

    for name in (
        "world_macro_sources.yaml",
        "world_macro_derivation_policy.yaml",
        "world_scope_mapping.yaml",
    ):
        (tmp_path / name).write_text((CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
    policy = yaml.safe_load((tmp_path / "world_macro_derivation_policy.yaml").read_text(encoding="utf-8"))
    original = policy["content_sha256"]
    policy["dimensions"]["rates_regime"]["rising_if_delta_pp_ge"] = 0.25
    hashed = dict(policy)
    hashed.pop("content_sha256")
    mutated = canonical_sha256(hashed)
    assert mutated != original
    policy["content_sha256"] = original
    (tmp_path / "world_macro_derivation_policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="content_sha256"):
        load_world_macro_operator_configs(config_dir=tmp_path)


@pytest.mark.parametrize(
    ("earlier", "later", "expected"),
    [
        (4.25, 4.375, "rising"),
        (4.25, 4.374, "stable"),
        (4.25, 4.125, "falling"),
        (4.25, 4.126, "stable"),
        (4.25, 4.25, "stable"),
    ],
)
def test_adapter_pipeline_derives_rate_regime_from_two_admissible_points(
    tmp_path: Path, earlier: float, later: float, expected: str
) -> None:
    run, store, _adapter = _collect_us(
        tmp_path=tmp_path,
        points=(("2026-08-21", earlier), ("2026-08-22", later)),
    )
    assert run.status == "completed"
    assert run.published_envelope is not None
    observation = run.published_envelope.observation
    assert observation.features["rates_regime"] == expected
    assert observation.features["macro_regime"] == "unknown"
    assert observation.features["usd_regime"] == "unknown"
    rates = next(item for item in observation.dimensions if item.dimension == "rates_regime")
    assert rates.value == expected
    assert rates.coverage_status == "complete"
    assert rates.method == "last_two_admissible_points_delta_pp"
    assert len(rates.fact_refs) == 2
    assert observation.coverage.status == "complete"
    assert {fact.period for fact in store.list_facts()} == {"2026-08-21", "2026-08-22"}
    for fact in store.list_facts():
        assert isinstance(fact.value, MacroNumericValue)
        assert fact.metric_key == "policy_rate"


def test_one_fresh_point_keeps_rates_unknown(tmp_path: Path) -> None:
    run, store, _adapter = _collect_us(tmp_path=tmp_path, points=(("2026-08-22", 4.33),))
    assert run.status == "completed"
    observation = run.published_envelope.observation
    assert observation.features["rates_regime"] == "unknown"
    rates = next(item for item in observation.dimensions if item.dimension == "rates_regime")
    assert rates.coverage_status == "unknown"
    assert rates.fact_refs == ()
    assert len(store.list_facts()) == 1
    assert observation.coverage.status == "complete"
    assert observation.coverage.fresh_sources == 1


def test_indexed_at_is_not_published_at_and_ttl_uses_period_vintage(tmp_path: Path) -> None:
    indexed = "2026-07-25T01:26:07.820Z"
    transport = ScriptedTransport(
        [_dbnomics_body(("2026-09-03", 3.63), indexed_at=indexed)]
    )
    facts = _ports(transport)["fed_funds_effective"].read_facts(RUN_SCOPE, CUTOFF)
    assert len(facts) == 1
    fact = facts[0]
    indexed_at = datetime(2026, 7, 25, 1, 26, 7, 820000, tzinfo=UTC)
    period_start = datetime(2026, 9, 3, tzinfo=UTC)
    assert fact.published_at != indexed_at
    assert fact.occurred_at == period_start
    assert fact.published_at == period_start
    assert fact.valid_until == period_start + timedelta(hours=72)
    assert fact.valid_until != indexed_at + timedelta(hours=72)
    assert fact.valid_until != CUTOFF + timedelta(hours=72)
    assert fact.ingested_at == CUTOFF


def test_stale_cpi_and_unemployment_are_not_renewed_on_refetch() -> None:
    clock = FakeClock(datetime(2026, 8, 24, 16, 47, tzinfo=UTC))
    cpi_body = _dbnomics_body(("2025-07", 148.149439018965), indexed_at="2025-08-31T11:54:51.710Z")
    unemp_body = _dbnomics_body(("2025-01", 4.0), indexed_at="2026-03-07T02:10:29.978Z")
    first = _ports(ScriptedTransport([cpi_body]), clock=clock)["cpi_us_imf"].read_facts(RUN_SCOPE, clock())
    unemp = _ports(ScriptedTransport([unemp_body]), clock=clock)["unemployment_rate_us"].read_facts(RUN_SCOPE, clock())
    cpi = first[0]
    jobless = unemp[0]
    assert cpi.period == "2025-07"
    assert cpi.valid_until == datetime(2025, 7, 1, tzinfo=UTC) + timedelta(days=40)
    assert cpi.valid_until < clock.now
    assert jobless.period == "2025-01"
    assert jobless.valid_until == datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=40)
    assert jobless.valid_until < clock.now
    clock.advance(hours=25)
    replayed = _ports(ScriptedTransport([cpi_body]), clock=clock)["cpi_us_imf"].read_facts(RUN_SCOPE, clock())
    assert replayed[0].period == cpi.period
    assert replayed[0].valid_until == cpi.valid_until
    assert replayed[0].valid_until != clock.now + timedelta(days=40)
    assert replayed[0].fact_version_id == cpi.fact_version_id


def test_dbnomics_observations_query_is_boolean_include_not_a_count() -> None:
    from trader.infrastructure.market_sources.world_macro.series import macro_source_fetch_url

    bundle = _bundle()
    entry = bundle.registry.entry_for("fed_funds_effective")
    url = macro_source_fetch_url(entry, bundle.budgets.providers["dbnomics"])
    assert "observations=1" in url or "observations=true" in url
    assert "observations=2" not in url
    transport = ScriptedTransport(
        [
            _dbnomics_body(
                ("2026-08-20", 4.10),
                ("2026-08-21", 4.25),
                ("2026-08-22", 4.50),
                indexed_at="2026-07-25T01:26:07.820Z",
            )
        ]
    )
    facts = _ports(transport)["fed_funds_effective"].read_facts(RUN_SCOPE, CUTOFF)
    assert [fact.period for fact in facts] == ["2026-08-21", "2026-08-22"]
    assert [fact.value.number for fact in facts] == [4.25, 4.50]


def test_no_lookahead_later_learned_points_do_not_rewrite_earlier_cutoff(tmp_path: Path) -> None:
    from trader.application.world_model.macro_pipeline import MacroWorldPipeline
    from trader.infrastructure.market_sources.world_macro import build_macro_source_ports

    bundle = _bundle()
    registry = _us_registry()
    target = MacroCollectionPlan.from_registry(registry).target_for(US_SCOPE)
    early_cutoff = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    late_cutoff = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
    clock = FakeClock(early_cutoff)
    store = WorldMacroStore(tmp_path, clock=clock)
    pipeline = MacroWorldPipeline(history=store, ledger=store, reader=store, policy=bundle.policy)
    one_point = _dbnomics_body(("2026-08-21", 4.25), indexed_at="2025-01-01T00:00:00Z")
    two_points = _dbnomics_body(("2026-08-21", 4.25), ("2026-08-22", 4.50), indexed_at="2025-01-01T00:00:00Z")
    first_ports = build_macro_source_ports(
        bundle, transport=ScriptedTransport([one_point]), clock=clock, sleeper=lambda _seconds: None
    )
    first = pipeline.collect(
        target=target,
        cutoff_at=early_cutoff,
        registry=registry,
        sources={"fed_funds_effective": first_ports["fed_funds_effective"]},
        observed_at=early_cutoff,
    )
    assert first.published_envelope is not None
    assert first.published_envelope.observation.features["rates_regime"] == "unknown"
    clock.now = late_cutoff
    second_ports = build_macro_source_ports(
        bundle, transport=ScriptedTransport([two_points]), clock=clock, sleeper=lambda _seconds: None
    )
    second = pipeline.collect(
        target=target,
        cutoff_at=late_cutoff,
        registry=registry,
        sources={"fed_funds_effective": second_ports["fed_funds_effective"]},
        observed_at=late_cutoff,
    )
    assert second.published_envelope is not None
    assert second.published_envelope.observation.features["rates_regime"] == "rising"
    selected_early = pipeline.select(US_SCOPE, early_cutoff)
    assert selected_early is not None
    assert selected_early.observation.features["rates_regime"] == "unknown"
    assert selected_early.observation.cutoff_at == early_cutoff


def test_cpi_index_does_not_manufacture_yoy_or_macro_regime(tmp_path: Path) -> None:
    from trader.application.world_model.macro_pipeline import MacroWorldPipeline
    from trader.infrastructure.market_sources.world_macro import build_macro_source_ports

    bundle = _bundle()
    entry = bundle.registry.entry_for("cpi_us_imf")
    registry = MacroSourceRegistry(registry_version=bundle.registry.registry_version, entries=(entry,))
    target = MacroCollectionPlan.from_registry(registry).target_for(US_SCOPE)
    clock = FakeClock(CUTOFF)
    store = WorldMacroStore(tmp_path, clock=clock)
    body = _dbnomics_body(("2026-07", 148.0), ("2026-08", 149.2), indexed_at="2025-01-01T00:00:00Z")
    ports = build_macro_source_ports(
        bundle, transport=ScriptedTransport([body]), clock=clock, sleeper=lambda _seconds: None
    )
    run = MacroWorldPipeline(history=store, ledger=store, reader=store, policy=bundle.policy).collect(
        target=target,
        cutoff_at=CUTOFF,
        registry=registry,
        sources={"cpi_us_imf": ports["cpi_us_imf"]},
        observed_at=CUTOFF,
    )
    observation = run.published_envelope.observation
    assert observation.features["macro_regime"] == "unknown"
    assert observation.features["rates_regime"] == "unknown"
    assert observation.features["usd_regime"] == "unknown"
    assert {fact.metric_key for fact in store.list_facts()} == {"cpi_index"}


def test_projector_does_not_mix_fed_and_ecb_or_double_vote() -> None:
    from trader.application.world_model.macro_pipeline import project_macro_world_observation

    def _rate(*, period: str, number: float, source_record_id: str, scope: MacroScope) -> MacroSourceFact:
        return MacroSourceFact(
            fact_kind="series_point",
            metric_key="policy_rate",
            scope=scope,
            value=MacroNumericValue(number=number, unit="percent"),
            period=period,
            occurred_at=f"{period}T00:00:00Z" if len(period) == 10 else f"{period}-01T00:00:00Z",
            published_at=f"{period}T00:00:00Z" if len(period) == 10 else f"{period}-01T00:00:00Z",
            ingested_at="2026-08-23T12:31:10Z",
            source={
                "provider_id": "dbnomics",
                "adapter_version": "world_dbnomics_series.v1",
                "source_record_id": source_record_id,
                "source_ref": "https://api.db.nomics.world/v22/series/example",
            },
            valid_until="2026-08-26T00:00:00Z",
        )

    fed_a = _rate(period="2026-08-21", number=4.25, source_record_id="FED/H15/RIFSPFF_N.D:2026-08-21", scope=US_SCOPE)
    fed_b = _rate(period="2026-08-22", number=4.50, source_record_id="FED/H15/RIFSPFF_N.D:2026-08-22", scope=US_SCOPE)
    ecb_a = _rate(period="2026-08-21", number=2.00, source_record_id="ECB/FM/B.U2.EUR.4F.KR.DFR.LEV:2026-08-21", scope=US_SCOPE)
    ecb_b = _rate(period="2026-08-22", number=2.50, source_record_id="ECB/FM/B.U2.EUR.4F.KR.DFR.LEV:2026-08-22", scope=US_SCOPE)
    mixed = project_macro_world_observation(
        policy=_committed_policy(),
        scope=US_SCOPE,
        cutoff_at=CUTOFF,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        facts=(fed_a, fed_b, ecb_a, ecb_b),
        expected_source_ids=("fed_funds_effective",),
        failed_source_ids=(),
        fresh_source_ids=("fed_funds_effective",),
    )
    assert mixed is not None
    assert mixed.features["rates_regime"] == "unknown"
    us_only = project_macro_world_observation(
        policy=_committed_policy(),
        scope=US_SCOPE,
        cutoff_at=CUTOFF,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        facts=(fed_a, fed_b),
        expected_source_ids=("fed_funds_effective",),
        failed_source_ids=(),
        fresh_source_ids=("fed_funds_effective",),
    )
    assert us_only is not None
    assert us_only.features["rates_regime"] == "rising"
    foreign = project_macro_world_observation(
        policy=_committed_policy(),
        scope=US_SCOPE,
        cutoff_at=CUTOFF,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        facts=(fed_a, fed_b, ecb_a),
        expected_source_ids=("fed_funds_effective",),
        failed_source_ids=(),
        fresh_source_ids=("fed_funds_effective",),
    )
    assert foreign is not None
    assert foreign.features["rates_regime"] == "unknown"


def test_wired_runtime_produces_known_rates_from_fresh_two_point_fixture(tmp_path: Path) -> None:
    from trader.runtime.world_macro_runtime import wire_world_macro_runtime

    fed = _dbnomics_body(("2026-08-21", 4.25), ("2026-08-22", 4.50), indexed_at="2026-07-25T01:26:07.820Z")
    ecb = _dbnomics_body(("2026-08-21", 2.25), ("2026-08-22", 2.00), indexed_at="2026-07-25T06:03:07.923Z")
    cpi = _dbnomics_body(("2025-07", 148.15), indexed_at="2025-08-31T11:54:51.710Z")
    hicp = _dbnomics_body(("2025-12", 129.55), indexed_at="2026-01-22T22:10:23.499Z")
    unemp = _dbnomics_body(("2025-01", 4.0), indexed_at="2026-03-07T02:10:29.978Z")
    by_url = {
        "https://api.db.nomics.world/v22/series/FED/H15/RIFSPFF_N.D?observations=1&metadata=0": fed,
        "https://api.db.nomics.world/v22/series/ECB/FM/D.U2.EUR.4F.KR.DFR.LEV?observations=1&metadata=0": ecb,
        "https://api.db.nomics.world/v22/series/IMF/CPI/M.US.PCPI_IX?observations=1&metadata=0": cpi,
        "https://api.db.nomics.world/v22/series/Eurostat/prc_hicp_midx/M.I15.CP00.EA20?observations=1&metadata=0": hicp,
        "https://api.db.nomics.world/v22/series/BLS/ln/LNS14000000?observations=1&metadata=0": unemp,
        "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?range=5d&interval=1d": _yahoo_body("2026-08-21", 91.22),
        "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1d": _yahoo_body("2026-08-21", 2401.0),
    }
    transport = ScriptedTransport(by_url=by_url)
    bundle = wire_world_macro_runtime(
        config_dir=CONFIG_DIR,
        state_dir=tmp_path,
        transport=transport,
        clock=FakeClock(CUTOFF),
        sleeper=lambda _seconds: None,
    )
    started = bundle.runner.trigger(now=CUTOFF, reason="numeric-derivation")
    assert started["triggered"] is True
    started["_thread"].join(timeout=15.0)
    assert started["_thread"].is_alive() is False
    bundle.runner.stop()
    store = WorldMacroStore(tmp_path / "world_macro", clock=lambda: CUTOFF)
    us = next(
        item
        for item in store.list_candidates_available_through(US_SCOPE, CUTOFF)
        if item.observation.cutoff_at == CUTOFF
    )
    eu = next(
        item
        for item in store.list_candidates_available_through(EU_SCOPE, CUTOFF)
        if item.observation.cutoff_at == CUTOFF
    )
    assert us.observation.features["rates_regime"] == "rising"
    assert eu.observation.features["rates_regime"] == "falling"
    assert us.observation.features["macro_regime"] == "unknown"
    assert us.observation.features["usd_regime"] == "unknown"
    assert eu.observation.features["usd_regime"] == "unknown"
    us_rates = next(item for item in us.observation.dimensions if item.dimension == "rates_regime")
    assert us_rates.method == "last_two_admissible_points_delta_pp"
    assert us.observation.coverage.status == "partial"
    assert "cpi_us_imf" in us.observation.coverage.missing_source_ids or us.observation.coverage.fresh_sources < 3


def _eu_registry() -> MacroSourceRegistry:
    bundle = _bundle()
    return MacroSourceRegistry(
        registry_version=bundle.registry.registry_version,
        entries=(bundle.registry.entry_for("ecb_deposit_rate"),),
    )


def test_ecb_registry_uses_daily_standing_key_rate_fill_not_change_dates_only() -> None:
    entry = _bundle().registry.entry_for("ecb_deposit_rate")
    assert entry.provider_entity_id == "ECB/FM/D.U2.EUR.4F.KR.DFR.LEV"
    assert entry.metric_key == "policy_rate"
    assert "B.U2" not in entry.provider_entity_id
    ttl = _bundle().ttl
    assert "ecb_deposit_rate" in ttl.until_superseded_source_ids
    assert "fed_funds_effective" not in ttl.until_superseded_source_ids
    assert ttl.policy_rate_until_superseded_d == 56
    assert ttl.series_point_daily_h == 72


def test_composed_ecb_standing_rate_is_admissible_and_stable_after_daily_72h_expires(
    tmp_path: Path,
) -> None:
    from trader.application.world_model.macro_pipeline import MacroWorldPipeline
    from trader.infrastructure.market_sources.world_macro import build_macro_source_ports

    cutoff = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    bundle = _bundle()
    clock = FakeClock(cutoff)
    store = WorldMacroStore(tmp_path, clock=clock)
    body = _dbnomics_body(("2026-09-04", 2.25), ("2026-09-05", 2.25), indexed_at="2026-07-25T06:03:07.923Z")
    ports = build_macro_source_ports(
        bundle, transport=ScriptedTransport([body]), clock=clock, sleeper=lambda _seconds: None
    )
    registry = _eu_registry()
    target = MacroCollectionPlan.from_registry(registry).target_for(EU_SCOPE)
    run = MacroWorldPipeline(history=store, ledger=store, reader=store, policy=bundle.policy).collect(
        target=target,
        cutoff_at=cutoff,
        registry=registry,
        sources={"ecb_deposit_rate": ports["ecb_deposit_rate"]},
        observed_at=cutoff,
    )
    assert run.status == "completed"
    observation = run.published_envelope.observation
    assert observation.features["rates_regime"] == "stable"
    facts = store.list_facts()
    assert {fact.period for fact in facts} == {"2026-09-04", "2026-09-05"}
    for fact in facts:
        assert fact.valid_until == fact.occurred_at + timedelta(days=56)
        assert fact.valid_until != fact.occurred_at + timedelta(hours=72)
        assert cutoff < fact.valid_until
        assert fact.source.source_record_id.startswith("ECB/FM/D.U2.EUR.4F.KR.DFR.LEV:")
        assert "B.U2" not in fact.source.source_record_id


def test_composed_fed_effective_daily_stays_stale_when_72h_period_window_elapsed(
    tmp_path: Path,
) -> None:
    cutoff = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    run, store, _adapter = _collect_us(
        tmp_path=tmp_path,
        cutoff=cutoff,
        clock=FakeClock(cutoff),
        points=(("2026-09-02", 3.63), ("2026-09-03", 3.63)),
    )
    assert run.status == "failed"
    assert run.published_envelope is None
    facts = store.list_facts()
    assert {fact.period for fact in facts} == {"2026-09-02", "2026-09-03"}
    for fact in facts:
        assert fact.valid_until == fact.occurred_at + timedelta(hours=72)
        assert cutoff >= fact.valid_until
