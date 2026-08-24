"""MACRO-4: typed DBnomics/Yahoo MacroSourcePort adapters and operator registry."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_macro import (
    MACRO_FACT_KINDS,
    MACRO_FEATURE_VALUES,
    MacroNumericValue,
    MacroScope,
    MacroSourceFact,
    MacroSourceRegistry,
)
from trader.domain.world_scope import WorldScopeMapping


UTC = timezone.utc
CONFIG_DIR = REPO_ROOT / "config"
OBSERVED_AT = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
RUN_SCOPE = MacroScope(kind="venue", entity_id="mic:XTAI")


@pytest.fixture(autouse=True)
def _forbid_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("live network is forbidden in MACRO-4 adapter tests")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


def _load_yaml(name: str) -> dict:
    payload = yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _dbnomics_body(period: str, value: float, *, indexed_at: str | None = "2026-08-23T12:30:00Z") -> str:
    doc: dict[str, object] = {"period": [period], "value": [value]}
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
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_first = False

    def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> object:
        from trader.infrastructure.market_sources.world_macro.series import MacroHttpResponse

        with self.lock:
            self.calls.append(url)
            call_index = len(self.calls)
        if self.block_first and call_index == 1:
            self.started.set()
            assert self.release.wait(timeout=2.0)
        if not self.script:
            raise AssertionError(f"unexpected extra HTTP call: {url}")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, MacroHttpResponse):
            return item
        if isinstance(item, tuple):
            status, body, headers_out = item
            return MacroHttpResponse(status=status, body=body, headers=headers_out)
        return MacroHttpResponse(status=200, body=str(item), headers={})


def _bundle():
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs

    return load_world_macro_operator_configs(config_dir=CONFIG_DIR)


def _ports(transport: ScriptedTransport, *, clock: FakeClock | None = None, sleeper=None):
    from trader.infrastructure.market_sources.world_macro import build_macro_source_ports

    return build_macro_source_ports(
        _bundle(),
        transport=transport,
        clock=clock or FakeClock(OBSERVED_AT),
        sleeper=sleeper or (lambda _seconds: None),
    )


def test_committed_registry_domain_and_operator_hashes_match() -> None:
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs

    raw = _load_yaml("world_macro_sources.yaml")
    bundle = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    domain = MacroSourceRegistry.from_mapping(
        {
            "schema_version": raw["schema_version"],
            "registry_version": raw["registry_version"],
            "entries": raw["entries"],
            "content_sha256": raw["content_sha256"],
        }
    )
    operator_payload = dict(raw)
    claimed_operator = operator_payload.pop("operator_config_sha256")
    assert domain.content_sha256 == raw["content_sha256"]
    assert canonical_sha256(operator_payload) == claimed_operator
    assert bundle.registry.content_sha256 == raw["content_sha256"]
    assert bundle.registry_operator_config_sha256 == claimed_operator
    assert bundle.registry.registry_version == "macro_sources.v1"


def test_committed_policy_full_content_hash_matches() -> None:
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs

    raw = _load_yaml("world_macro_derivation_policy.yaml")
    bundle = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    payload = dict(raw)
    claimed = payload.pop("content_sha256")
    assert canonical_sha256(payload) == claimed
    assert bundle.policy_content_sha256 == claimed
    assert bundle.policy.transform_version == "macro_regimes.v1"
    assert bundle.policy.producer_version == "macro_source_only.v2"
    for dimension, values in raw["vocabularies"].items():
        assert "unknown" in values
        assert frozenset(values) == MACRO_FEATURE_VALUES[dimension]


def test_committed_scope_mapping_domain_and_operator_hashes_match() -> None:
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs

    raw = _load_yaml("world_scope_mapping.yaml")
    bundle = load_world_macro_operator_configs(config_dir=CONFIG_DIR)
    domain = WorldScopeMapping.from_mapping(
        {
            "schema_version": raw["schema_version"],
            "mapping_id": raw["mapping_id"],
            "entries": raw["entries"],
            "content_sha256": raw["content_sha256"],
        }
    )
    operator_payload = dict(raw)
    claimed_operator = operator_payload.pop("operator_config_sha256")
    assert domain.content_sha256 == raw["content_sha256"]
    assert canonical_sha256(operator_payload) == claimed_operator
    assert bundle.scope_mapping.content_sha256 == raw["content_sha256"]
    assert bundle.scope_operator_config_sha256 == claimed_operator


def test_load_rejects_tampered_registry_and_policy_hashes(tmp_path: Path) -> None:
    from trader.infrastructure.market_sources.world_macro import load_world_macro_operator_configs

    for name in (
        "world_macro_sources.yaml",
        "world_macro_derivation_policy.yaml",
        "world_scope_mapping.yaml",
    ):
        (tmp_path / name).write_text((CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")

    sources = yaml.safe_load((tmp_path / "world_macro_sources.yaml").read_text(encoding="utf-8"))
    sources["content_sha256"] = "0" * 64
    (tmp_path / "world_macro_sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")
    with pytest.raises(ValueError, match="content_sha256"):
        load_world_macro_operator_configs(config_dir=tmp_path)

    (tmp_path / "world_macro_sources.yaml").write_text(
        (CONFIG_DIR / "world_macro_sources.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sources = yaml.safe_load((tmp_path / "world_macro_sources.yaml").read_text(encoding="utf-8"))
    sources["operator_config_sha256"] = "1" * 64
    (tmp_path / "world_macro_sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")
    with pytest.raises(ValueError, match="operator_config_sha256"):
        load_world_macro_operator_configs(config_dir=tmp_path)

    (tmp_path / "world_macro_sources.yaml").write_text(
        (CONFIG_DIR / "world_macro_sources.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    policy = yaml.safe_load((tmp_path / "world_macro_derivation_policy.yaml").read_text(encoding="utf-8"))
    policy["content_sha256"] = "2" * 64
    (tmp_path / "world_macro_derivation_policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="content_sha256"):
        load_world_macro_operator_configs(config_dir=tmp_path)


def test_registry_declares_structured_gaps_and_excludes_event_sources() -> None:
    bundle = _bundle()
    source_ids = {entry.source_id for entry in bundle.registry.entries}
    providers = {entry.provider_id for entry in bundle.registry.entries}
    kinds = {entry.fact_kind for entry in bundle.registry.entries}
    assert bundle.declared_gaps == (
        "broad_usd_index",
        "taiwan_policy_rate",
        "taiwan_cpi",
        "growth_series",
    )
    assert source_ids.isdisjoint(bundle.declared_gaps)
    assert "broad_usd_index" not in source_ids
    assert providers == {"dbnomics", "yahoo_finance"}
    assert kinds <= MACRO_FACT_KINDS
    assert "gdelt" in bundle.excluded_providers
    assert "news_macro_brief" in bundle.excluded_providers
    assert bundle.budgets.fetch_in_run_cycle is False
    assert bundle.budgets.retry_max == 1
    assert bundle.budgets.honor_retry_after is True
    assert bundle.budgets.worker == "single_flight"


def test_dbnomics_adapter_emits_typed_fact_for_canonical_scope_not_run_scope() -> None:
    transport = ScriptedTransport([_dbnomics_body("2026-08-22", 4.33)])
    ports = _ports(transport)
    facts = ports["fed_funds_effective"].read_facts(RUN_SCOPE, OBSERVED_AT)
    assert len(facts) == 1
    fact = facts[0]
    assert isinstance(fact, MacroSourceFact)
    assert fact.scope == MacroScope(kind="country", entity_id="iso-3166:US")
    assert fact.fact_kind == "series_point"
    assert fact.metric_key == "policy_rate"
    assert fact.period == "2026-08-22"
    assert fact.value == MacroNumericValue(number=4.33, unit="percent")
    assert fact.source.provider_id == "dbnomics"
    assert fact.source.adapter_version == "dbnomics_series.v1"
    published = datetime(2026, 8, 23, 12, 30, tzinfo=UTC)
    assert fact.published_at == published
    assert fact.valid_until == published + timedelta(hours=72)
    assert fact.valid_until != OBSERVED_AT + timedelta(hours=72)
    assert "XTAI" not in json.dumps(fact.to_dict())
    assert transport.calls == [
        "https://api.db.nomics.world/v22/series/FED/H15/RIFSPFF_N.D?observations=1&metadata=0"
    ]


def test_dbnomics_urls_add_metadata_zero_and_keep_period_value_indexed_at() -> None:
    bundle = _bundle()
    db_entries = [entry for entry in bundle.registry.entries if entry.provider_id == "dbnomics"]
    assert db_entries
    indexed_at = "2026-08-23T12:30:00Z"
    transport = ScriptedTransport(
        [_dbnomics_body("2026-08-22", 4.33, indexed_at=indexed_at) for _ in db_entries]
    )
    ports = _ports(transport)
    for entry in db_entries:
        facts = ports[entry.source_id].read_facts(RUN_SCOPE, OBSERVED_AT)
        assert facts[0].period == "2026-08-22"
        assert facts[0].value.number == 4.33
        assert facts[0].published_at == datetime(2026, 8, 23, 12, 30, tzinfo=UTC)
    assert transport.calls
    assert all("metadata=0" in url for url in transport.calls)
    assert all("observations=" in url for url in transport.calls)
    assert all("metadata=0" not in url.split("?")[0] for url in transport.calls)


def test_dbnomics_monthly_cpi_uses_monthly_ttl_and_index_unit() -> None:
    transport = ScriptedTransport([_dbnomics_body("2026-07", 148.2)])
    facts = _ports(transport)["cpi_us_imf"].read_facts(RUN_SCOPE, OBSERVED_AT)
    assert facts[0].metric_key == "cpi_index"
    assert facts[0].value.unit == "index"
    assert facts[0].occurred_at == datetime(2026, 7, 1, tzinfo=UTC)
    published = datetime(2026, 8, 23, 12, 30, tzinfo=UTC)
    assert facts[0].published_at == published
    assert facts[0].valid_until == published + timedelta(days=40)
    assert facts[0].valid_until != OBSERVED_AT + timedelta(days=40)


def test_yahoo_commodity_adapter_emits_front_month_benchmark() -> None:
    transport = ScriptedTransport([_yahoo_body("2026-08-21", 91.22)])
    facts = _ports(transport)["brent_crude_usd"].read_facts(RUN_SCOPE, OBSERVED_AT)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.scope == MacroScope(kind="world", entity_id="market")
    assert fact.fact_kind == "market_benchmark"
    assert fact.metric_key == "brent_front_month_usd"
    assert fact.period == "2026-08-21"
    assert fact.value == MacroNumericValue(number=91.22, unit="usd")
    assert fact.source.provider_id == "yahoo_finance"
    assert fact.source.adapter_version == "yahoo_commodity.v1"
    published = datetime(2026, 8, 21, tzinfo=UTC)
    assert fact.published_at == published
    assert fact.valid_until == published + timedelta(hours=72)
    assert fact.valid_until != OBSERVED_AT + timedelta(hours=72)
    assert transport.calls[0].endswith("BZ=F?range=5d&interval=1d")


def test_period_correction_keeps_fact_key_and_supersedes_previous_leaf() -> None:
    transport = ScriptedTransport(
        [
            _dbnomics_body("2026-08", 4.25, indexed_at="2026-08-20T12:00:00Z"),
            _dbnomics_body("2026-08", 4.50, indexed_at="2026-08-23T12:00:00Z"),
        ]
    )
    clock = FakeClock(OBSERVED_AT)
    ports = _ports(transport, clock=clock)
    first = ports["ecb_deposit_rate"].read_facts(RUN_SCOPE, clock())
    clock.advance(hours=25)
    second = ports["ecb_deposit_rate"].read_facts(RUN_SCOPE, clock())
    assert first[0].fact_key == second[0].fact_key
    assert second[0].fact_version_id != first[0].fact_version_id
    assert second[0].supersedes_fact_version_id == first[0].fact_version_id.value
    assert second[0].value == MacroNumericValue(number=4.50, unit="percent")


def test_same_period_value_and_unit_is_replayed_not_a_new_vintage() -> None:
    transport = ScriptedTransport(
        [
            _dbnomics_body("2026-08-22", 4.33, indexed_at="2026-08-23T12:30:00Z"),
            _dbnomics_body("2026-08-22", 4.33, indexed_at="2026-08-23T12:30:00Z"),
        ]
    )
    clock = FakeClock(OBSERVED_AT)
    ports = _ports(transport, clock=clock)
    first = ports["fed_funds_effective"].read_facts(RUN_SCOPE, clock())
    clock.advance(hours=25)
    second = ports["fed_funds_effective"].read_facts(RUN_SCOPE, clock())
    assert first[0].fact_version_id == second[0].fact_version_id
    assert second[0].supersedes_fact_version_id is None


def test_adapter_reconstruction_replays_identical_external_data_across_runtime_restarts(tmp_path: Path) -> None:
    from trader.infrastructure.state_db.world_macro_store import WorldMacroStore

    body = _dbnomics_body("2026-08-22", 4.33, indexed_at="2026-08-23T12:30:00Z")
    first_observed = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
    later_observed = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    published = datetime(2026, 8, 23, 12, 30, tzinfo=UTC)
    first = _ports(ScriptedTransport([body]), clock=FakeClock(first_observed))["fed_funds_effective"].read_facts(
        RUN_SCOPE, first_observed
    )
    second = _ports(ScriptedTransport([body]), clock=FakeClock(later_observed))["fed_funds_effective"].read_facts(
        RUN_SCOPE, later_observed
    )
    assert first[0].fact_version_id == second[0].fact_version_id
    assert first[0].content_sha256 == second[0].content_sha256
    assert first[0].valid_until == second[0].valid_until == published + timedelta(hours=72)
    assert first[0].valid_until != later_observed + timedelta(hours=72)
    assert first[0].ingested_at == first_observed
    assert second[0].ingested_at == later_observed
    store = WorldMacroStore(tmp_path, clock=lambda: first_observed)
    first_ref = store.append_fact(first[0])
    replayed = store.append_fact(second[0])
    assert replayed.receipt.receipt_id == first_ref.receipt.receipt_id
    assert replayed.receipt.ready_at == first_ref.receipt.ready_at == first_observed
    history = tmp_path / "facts" / "2026-08-23.jsonl"
    rows = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    loaded = MacroSourceFact.from_mapping(rows[0])
    assert loaded.ingested_at == first_observed
    assert loaded.valid_until == published + timedelta(hours=72)
    assert loaded.content_sha256 == first[0].content_sha256


def test_late_fetch_does_not_extend_valid_until_of_already_expired_provider_data() -> None:
    published = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    observed = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
    facts = _ports(
        ScriptedTransport([_dbnomics_body("2026-08-20", 4.33, indexed_at="2026-08-20T12:00:00Z")])
    )["fed_funds_effective"].read_facts(RUN_SCOPE, observed)
    assert facts[0].published_at == published
    assert facts[0].valid_until == published + timedelta(hours=72)
    assert facts[0].valid_until < observed
    assert facts[0].valid_until != observed + timedelta(hours=72)


def test_urllib_macro_transport_get_uses_mocked_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    from trader.infrastructure.market_sources.world_macro.series import UrllibMacroTransport

    class _Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self) -> bytes:
            return b'{"ok": true}'

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    seen: dict[str, object] = {}

    def fake_urlopen(req: object, timeout: object = None) -> object:
        seen["url"] = getattr(req, "full_url", None)
        seen["timeout"] = timeout
        seen["headers"] = dict(getattr(req, "headers", {}))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport = UrllibMacroTransport()
    response = transport.get("https://example.test/series", timeout_s=9.0, headers={"User-Agent": "casys-test"})
    assert response.status == 200
    assert response.body == b'{"ok": true}'
    assert seen["url"] == "https://example.test/series"
    assert seen["timeout"] == 9.0
    assert "casys-test" in str(seen["headers"])


def test_empty_or_unparsable_payload_is_explicit_missing_not_a_zero() -> None:
    from trader.infrastructure.market_sources.world_macro.series import MacroSourceFetchError

    transport = ScriptedTransport([json.dumps({"series": {"docs": []}})])
    with pytest.raises(MacroSourceFetchError, match="unavailable"):
        _ports(transport)["unemployment_rate_us"].read_facts(RUN_SCOPE, OBSERVED_AT)


def test_retry_after_cannot_exceed_provider_timeout() -> None:
    from trader.infrastructure.market_sources.world_macro.series import (
        MacroHttpResponse,
        MacroSourceFetchError,
    )

    sleeps: list[float] = []
    transport = ScriptedTransport(
        [
            MacroHttpResponse(status=429, body="rate limited", headers={"Retry-After": "86400"}),
            MacroHttpResponse(status=429, body="still limited", headers={"Retry-After": "86400"}),
        ]
    )
    with pytest.raises(MacroSourceFetchError, match="429"):
        _ports(transport, sleeper=sleeps.append)["fed_funds_effective"].read_facts(RUN_SCOPE, OBSERVED_AT)
    assert sleeps
    assert all(delay <= 5.0 for delay in sleeps)


def test_provider_rate_limiter_uses_transport_timeout_without_extra_threads() -> None:
    import inspect

    from trader.infrastructure.market_sources.world_macro.series import (
        MacroHttpResponse,
        ProviderRateLimiter,
    )

    seen: dict[str, float] = {}

    class Capture:
        def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> MacroHttpResponse:
            del url, headers
            seen["timeout_s"] = timeout_s
            return MacroHttpResponse(status=200, body="{}", headers={})

    limiter = ProviderRateLimiter(
        min_interval_s=0.0,
        retry_max=0,
        honor_retry_after=True,
        timeout_s=0.2,
        transport=Capture(),  # type: ignore[arg-type]
        clock=lambda: OBSERVED_AT,
        sleeper=lambda _seconds: None,
    )
    response = limiter.get("https://example.test/ok")
    assert response.status == 200
    assert seen["timeout_s"] == 0.2
    source = inspect.getsource(ProviderRateLimiter)
    assert "threading.Thread" not in source
    assert "_bounded_transport_get" not in source


def test_http_429_honors_retry_after_retries_once_then_missing() -> None:
    from trader.infrastructure.market_sources.world_macro.series import (
        MacroHttpResponse,
        MacroSourceFetchError,
    )

    sleeps: list[float] = []
    transport = ScriptedTransport(
        [
            MacroHttpResponse(status=429, body="rate limited", headers={"Retry-After": "7"}),
            MacroHttpResponse(status=429, body="still limited", headers={"Retry-After": "9"}),
            MacroHttpResponse(status=200, body=_dbnomics_body("2026-08-22", 4.33), headers={}),
        ]
    )
    with pytest.raises(MacroSourceFetchError, match="429"):
        _ports(transport, sleeper=sleeps.append)["fed_funds_effective"].read_facts(RUN_SCOPE, OBSERVED_AT)
    assert sleeps == [5.0]
    assert len(transport.calls) == 2


def test_timeout_retries_once_then_missing() -> None:
    from trader.infrastructure.market_sources.world_macro.series import MacroSourceFetchError

    transport = ScriptedTransport([TimeoutError("timed out"), TimeoutError("timed out again")])
    with pytest.raises((TimeoutError, MacroSourceFetchError), match="timeout"):
        _ports(transport)["gold_usd"].read_facts(RUN_SCOPE, OBSERVED_AT)
    assert len(transport.calls) == 2


def test_source_deadline_covers_min_interval_attempts_and_bounded_retry_wait() -> None:
    from trader.infrastructure.market_sources.world_macro.series import source_deadline_s

    bundle = _bundle()
    db = bundle.budgets.providers["dbnomics"]
    yahoo = bundle.budgets.providers["yahoo_finance"]
    db_deadline = source_deadline_s(
        db,
        retry_max=bundle.budgets.retry_max,
        honor_retry_after=bundle.budgets.honor_retry_after,
    )
    yahoo_deadline = source_deadline_s(
        yahoo,
        retry_max=bundle.budgets.retry_max,
        honor_retry_after=bundle.budgets.honor_retry_after,
    )
    assert bundle.budgets.retry_max == 1
    assert bundle.budgets.honor_retry_after is True
    assert db_deadline == db.min_interval_s + 2 * db.timeout_s + db.timeout_s
    assert yahoo_deadline == yahoo.min_interval_s + 2 * yahoo.timeout_s + yahoo.timeout_s
    assert db_deadline == 17.0
    assert yahoo_deadline == 29.0
    assert db_deadline > db.timeout_s
    assert yahoo_deadline > yahoo.timeout_s


def _deadline_limiter(*, transport, timeout_s: float, min_interval_s: float, retry_max: int = 1):
    from types import SimpleNamespace

    from trader.infrastructure.market_sources.world_macro.series import (
        ProviderRateLimiter,
        source_deadline_s,
    )

    budget = SimpleNamespace(timeout_s=timeout_s, min_interval_s=min_interval_s)
    deadline_s = source_deadline_s(budget, retry_max=retry_max, honor_retry_after=True)
    limiter = ProviderRateLimiter(
        min_interval_s=min_interval_s,
        retry_max=retry_max,
        honor_retry_after=True,
        timeout_s=timeout_s,
        deadline_s=deadline_s,
        transport=transport,
        clock=lambda: OBSERVED_AT,
        sleeper=time.sleep,
    )
    return limiter, deadline_s


def test_limiter_wait_plus_slow_valid_response_fits_derived_deadline() -> None:
    from trader.application.world_model.source_deadline import BoundedSourceDeadline
    from trader.infrastructure.market_sources.world_macro.series import MacroHttpResponse

    timeout_s = 0.18
    min_interval_s = 0.12
    transport_timeouts: list[float] = []

    class SlowOk:
        def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> MacroHttpResponse:
            del url, headers
            transport_timeouts.append(timeout_s)
            time.sleep(0.14)
            return MacroHttpResponse(status=200, body=_dbnomics_body("2026-08-22", 4.33), headers={})

    limiter, deadline_s = _deadline_limiter(
        transport=SlowOk(),
        timeout_s=timeout_s,
        min_interval_s=min_interval_s,
    )
    assert deadline_s > timeout_s
    assert limiter.get("https://example.test/prime").status == 200
    started = time.monotonic()
    response = BoundedSourceDeadline(max_workers=1).run(
        lambda: limiter.get("https://example.test/slow"),
        timeout_s=deadline_s,
    )
    elapsed = time.monotonic() - started
    assert response.status == 200
    assert elapsed > timeout_s
    assert elapsed < deadline_s
    assert transport_timeouts == [timeout_s, timeout_s]


def test_limiter_timeout_retries_within_derived_deadline() -> None:
    from trader.application.world_model.source_deadline import BoundedSourceDeadline
    from trader.infrastructure.market_sources.world_macro.series import MacroSourceFetchError

    timeout_s = 0.16
    min_interval_s = 0.08
    calls: list[str] = []

    class SlowTimeout:
        def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> object:
            del timeout_s, headers
            calls.append(url)
            time.sleep(0.10)
            raise TimeoutError("timed out")

    limiter, deadline_s = _deadline_limiter(
        transport=SlowTimeout(),
        timeout_s=timeout_s,
        min_interval_s=min_interval_s,
    )
    with pytest.raises(MacroSourceFetchError) as caught:
        BoundedSourceDeadline(max_workers=1).run(
            lambda: limiter.get("https://example.test/timeout"),
            timeout_s=deadline_s,
        )
    assert caught.value.reason == "timeout"
    assert calls == ["https://example.test/timeout", "https://example.test/timeout"]


def test_limiter_429_retry_fits_derived_deadline() -> None:
    from trader.application.world_model.source_deadline import BoundedSourceDeadline
    from trader.infrastructure.market_sources.world_macro.series import (
        MacroHttpResponse,
        MacroSourceFetchError,
    )

    timeout_s = 0.16
    min_interval_s = 0.08
    script = [
        MacroHttpResponse(status=429, body="rate limited", headers={"Retry-After": "1"}),
        MacroHttpResponse(status=200, body=_dbnomics_body("2026-08-22", 4.33), headers={}),
    ]

    class Scripted:
        def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> MacroHttpResponse:
            del url, timeout_s, headers
            item = script.pop(0)
            return item

    limiter, deadline_s = _deadline_limiter(
        transport=Scripted(),
        timeout_s=timeout_s,
        min_interval_s=min_interval_s,
    )
    started = time.monotonic()
    response = BoundedSourceDeadline(max_workers=1).run(
        lambda: limiter.get("https://example.test/retry"),
        timeout_s=deadline_s,
    )
    elapsed = time.monotonic() - started
    assert response.status == 200
    assert elapsed > timeout_s
    assert elapsed < deadline_s
    assert script == []

    exhausted = [
        MacroHttpResponse(status=429, body="rate limited", headers={"Retry-After": "1"}),
        MacroHttpResponse(status=429, body="still limited", headers={"Retry-After": "1"}),
    ]

    class Exhausted:
        def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> MacroHttpResponse:
            del url, timeout_s, headers
            return exhausted.pop(0)

    limiter_429, deadline_429 = _deadline_limiter(
        transport=Exhausted(),
        timeout_s=timeout_s,
        min_interval_s=min_interval_s,
    )
    with pytest.raises(MacroSourceFetchError) as caught:
        BoundedSourceDeadline(max_workers=1).run(
            lambda: limiter_429.get("https://example.test/exhausted"),
            timeout_s=deadline_429,
        )
    assert caught.value.reason == "http_429"


def test_provider_requests_are_single_flight_and_coalesced() -> None:
    transport = ScriptedTransport([_dbnomics_body("2026-08-22", 4.33)])
    transport.block_first = True
    ports = _ports(transport)
    results: list[tuple[MacroSourceFact, ...]] = []

    def _call() -> None:
        results.append(ports["fed_funds_effective"].read_facts(RUN_SCOPE, OBSERVED_AT))

    first = threading.Thread(target=_call)
    second = threading.Thread(target=_call)
    first.start()
    assert transport.started.wait(timeout=2.0)
    second.start()
    transport.release.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)
    assert len(transport.calls) == 1
    assert len(results) == 2
    assert results[0][0].fact_version_id == results[1][0].fact_version_id


def test_cooldown_24h_skips_provider_fetch() -> None:
    transport = ScriptedTransport(
        [
            _yahoo_body("2026-08-21", 91.22),
            _yahoo_body("2026-08-22", 92.0),
        ]
    )
    clock = FakeClock(OBSERVED_AT)
    ports = _ports(transport, clock=clock)
    first = ports["brent_crude_usd"].read_facts(RUN_SCOPE, clock())
    clock.advance(hours=23)
    second = ports["brent_crude_usd"].read_facts(RUN_SCOPE, clock())
    assert len(transport.calls) == 1
    assert first[0].value.number == second[0].value.number == 91.22
    clock.advance(hours=2)
    third = ports["brent_crude_usd"].read_facts(RUN_SCOPE, clock())
    assert len(transport.calls) == 2
    assert third[0].value.number == 92.0


def test_min_interval_is_applied_between_serialized_provider_requests() -> None:
    sleeps: list[float] = []
    clock = FakeClock(OBSERVED_AT)
    transport = ScriptedTransport(
        [
            _dbnomics_body("2026-08-22", 4.33),
            _dbnomics_body("2026-07", 148.2),
        ]
    )
    ports = _ports(transport, clock=clock, sleeper=sleeps.append)
    ports["fed_funds_effective"].read_facts(RUN_SCOPE, clock())
    ports["cpi_us_imf"].read_facts(RUN_SCOPE, clock())
    assert sleeps == [2.0]


def test_adapters_do_not_import_gdelt_or_news_macro_brief() -> None:
    world_macro_dir = REPO_ROOT / "trader" / "infrastructure" / "market_sources" / "world_macro"
    assert (world_macro_dir / "__init__.py").is_file()
    assert (world_macro_dir / "series.py").is_file()
    for path in sorted(world_macro_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "NewsMacroBrief" not in text
        assert "gdelt" not in text.lower()
        assert "calendar_event" not in text
        assert "global_event" not in text


def test_run_cycle_does_not_call_world_macro_adapters() -> None:
    daemon = (REPO_ROOT / "trader" / "runtime" / "daemon.py").read_text(encoding="utf-8")
    assert "market_sources.world_macro" not in daemon
    assert "DBnomicsSeriesAdapter" not in daemon
    assert "YahooCommodityAdapter" not in daemon
    assert "build_macro_source_ports" not in daemon
    cycle = (REPO_ROOT / "trader" / "runtime" / "cycle_finalization.py").read_text(encoding="utf-8")
    assert "world_macro" not in cycle


def test_build_ports_covers_every_registry_entry_and_no_gap_source() -> None:
    transport = ScriptedTransport([])
    ports = _ports(transport)
    bundle = _bundle()
    assert set(ports) == {entry.source_id for entry in bundle.registry.entries}
    assert set(ports).isdisjoint(bundle.declared_gaps)
