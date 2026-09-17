from __future__ import annotations

import ast
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_company import (
    COMPANY_PRODUCER_VERSION,
    COMPANY_TRANSFORM_VERSION,
    DriverCompanyBundle,
)
from trader.domain.world_driver import (
    DRIVER_REGIME_BUNDLE_SCHEMA,
    DRIVER_STATE_SCHEMA,
    DriverRegimeBundle,
    DriverState,
)
from trader.domain.world_news import (
    NEWS_PRODUCER_VERSION,
    NEWS_TRANSFORM_VERSION,
    DriverNewsBundle,
)
from trader.domain.world_macro import (
    MACRO_COVERAGE_STATUSES,
    MACRO_FEATURE_VALUES,
    MACRO_PRODUCER_VERSION,
    MACRO_SOURCE_REGISTRY_VERSION,
    MACRO_TRANSFORM_VERSION,
    MacroCoverage,
    MacroDimensionState,
    MacroFactSource,
    MacroNumericValue,
    MacroScope,
    MacroSourceFact,
    MacroWorldObservation,
)


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_driver.py"


def _bundle(**overrides: object) -> DriverRegimeBundle:
    values: dict[str, object] = {
        "macro_regime": "unknown",
        "rates_regime": "rising",
        "usd_regime": "unknown",
        "coverage_status": "partial",
    }
    values.update(overrides)
    return DriverRegimeBundle(**values)  # type: ignore[arg-type]


def _regime_state(**overrides: object) -> DriverState:
    values: dict[str, object] = {
        "source_family": "macro_observation",
        "signal_class": "regime_bundle",
        "regimes": _bundle(),
        "artifact_kind": "macro_world_observation",
        "until_bound": "bounded",
        "producer_version": MACRO_PRODUCER_VERSION,
        "transform_version": MACRO_TRANSFORM_VERSION,
        "missingness": "none",
    }
    values.update(overrides)
    return DriverState(**values)  # type: ignore[arg-type]


def _fact() -> MacroSourceFact:
    return MacroSourceFact(
        fact_kind="series_point",
        metric_key="policy_rate",
        scope=MacroScope(kind="country", entity_id="iso-3166:US"),
        value=MacroNumericValue(number=4.25, unit="percent"),
        period="2026-08",
        occurred_at="2026-08-23T00:00:00Z",
        published_at="2026-08-23T12:30:00Z",
        ingested_at="2026-08-23T12:31:10Z",
        source=MacroFactSource(
            provider_id="dbnomics",
            adapter_version="world_dbnomics_series.v1",
            source_record_id="stable-provider-id",
            source_ref="https://source.example/record",
        ),
    )


def _dimension(
    *,
    dimension: str,
    value: str,
    coverage_status: str = "complete",
    fact_refs: tuple[str, ...] = (),
) -> MacroDimensionState:
    return MacroDimensionState(
        dimension=dimension,
        value=value,
        coverage_status=coverage_status,
        method=MACRO_TRANSFORM_VERSION,
        fact_refs=fact_refs,
    )


def _observation(**overrides: object) -> MacroWorldObservation:
    fact = _fact()
    fact_ref = fact.fact_version_id.value
    values: dict[str, object] = {
        "scope": fact.scope,
        "cutoff_at": CUTOFF,
        "producer_version": MACRO_PRODUCER_VERSION,
        "transform_version": MACRO_TRANSFORM_VERSION,
        "source_registry_version": MACRO_SOURCE_REGISTRY_VERSION,
        "fact_refs": (fact_ref,),
        "features": {"macro_regime": "unknown", "rates_regime": "rising", "usd_regime": "unknown"},
        "dimensions": (
            _dimension(dimension="macro_regime", value="unknown", coverage_status="unknown"),
            _dimension(dimension="rates_regime", value="rising", fact_refs=(fact_ref,)),
            _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown"),
        ),
        "coverage": MacroCoverage(
            status="partial",
            required_sources=3,
            fresh_sources=1,
            missing_source_ids=("broad_usd_index", "cpi_yoy"),
        ),
        "valid_until": VALID_UNTIL,
    }
    values.update(overrides)
    return MacroWorldObservation(**values)  # type: ignore[arg-type]


def test_regime_bundle_reuses_closed_vocab_and_round_trips() -> None:
    bundle = _bundle()
    assert bundle.schema_version == DRIVER_REGIME_BUNDLE_SCHEMA
    assert bundle.macro_regime in MACRO_FEATURE_VALUES["macro_regime"]
    assert bundle.rates_regime in MACRO_FEATURE_VALUES["rates_regime"]
    assert bundle.usd_regime in MACRO_FEATURE_VALUES["usd_regime"]
    assert bundle.coverage_status in MACRO_COVERAGE_STATUSES
    replayed = DriverRegimeBundle.from_mapping(bundle.to_dict())
    assert replayed == bundle
    assert replayed.identity_tuple() == ("unknown", "rising", "unknown", "partial")
    with pytest.raises(ValueError, match="macro_regime"):
        _bundle(macro_regime="hawkish")
    with pytest.raises(ValueError, match="coverage_status"):
        _bundle(coverage_status="sparse")
    with pytest.raises(ValueError, match="schema_version"):
        DriverRegimeBundle.from_mapping({**bundle.to_dict(), "schema_version": "driver_regime_bundle.v2"})


def test_producer_unknown_is_not_missing_or_unspecified() -> None:
    reported_unknown = DriverState.from_observation(_observation())
    assert reported_unknown.signal_class == "regime_bundle"
    assert reported_unknown.missingness == "none"
    assert reported_unknown.regimes is not None
    assert reported_unknown.regimes.macro_regime == "unknown"
    assert reported_unknown.regimes.usd_regime == "unknown"
    unjoined = DriverState.missing(missingness="observation_unjoined")
    unspecified = DriverState.unspecified(artifact_kind="news_macro")
    assert unjoined.signal_class == "missing"
    assert unjoined.regimes is None
    assert unspecified.signal_class == "unspecified"
    assert unspecified.missingness == "not_applicable"
    assert reported_unknown.identity_tuple() != unjoined.identity_tuple()
    assert reported_unknown.identity_tuple() != unspecified.identity_tuple()
    assert unjoined.identity_tuple() != unspecified.identity_tuple()


def test_from_observation_and_explicit_factories_round_trip() -> None:
    bounded = DriverState.from_observation(_observation())
    assert bounded.until_bound == "bounded"
    assert bounded.producer_version == MACRO_PRODUCER_VERSION
    assert bounded.transform_version == MACRO_TRANSFORM_VERSION
    assert bounded.artifact_kind == "macro_world_observation"
    assert DriverState.from_mapping(bounded.to_dict()) == bounded
    unbounded = DriverState.from_observation(_observation(valid_until=None))
    assert unbounded.until_bound == "unbounded"
    assert unbounded.identity_tuple() != bounded.identity_tuple()
    with pytest.raises(TypeError, match="proven MacroWorldObservation"):
        DriverState.from_observation(bounded.to_dict())  # type: ignore[arg-type]
    missing = DriverState.missing(
        missingness="observation_hash_mismatch",
        artifact_kind="macro_world_observation",
    )
    assert DriverState.from_mapping(missing.to_dict()) == missing
    unspecified = DriverState.unspecified(source_family="unproven", until_bound="unknown")
    assert DriverState.from_mapping(unspecified.to_dict()) == unspecified
    assert missing.schema_version == DRIVER_STATE_SCHEMA


def test_closed_invariants_reject_crossed_signal_classes() -> None:
    with pytest.raises(ValueError, match="regime_bundle"):
        _regime_state(regimes=None)
    with pytest.raises(ValueError, match="missingness"):
        _regime_state(missingness="observation_unjoined")
    with pytest.raises(ValueError, match="source_family"):
        _regime_state(source_family="knowledge_artifact")
    with pytest.raises(ValueError, match="missing"):
        DriverState.missing(missingness="none")
    with pytest.raises(ValueError, match="regime bundle"):
        DriverState(
            source_family="macro_observation",
            signal_class="missing",
            regimes=_bundle(),
            artifact_kind=None,
            until_bound="unknown",
            producer_version="unspecified",
            transform_version="unspecified",
            missingness="observation_unjoined",
        )
    with pytest.raises(ValueError, match="unspecified"):
        DriverState(
            source_family="knowledge_artifact",
            signal_class="unspecified",
            regimes=None,
            artifact_kind="news_macro",
            until_bound="unknown",
            producer_version="unspecified",
            transform_version="unspecified",
            missingness="artifact_unjoined",
        )
    with pytest.raises(ValueError, match="producer_not_admitted|raw"):
        DriverState.missing(
            missingness="producer_not_admitted",
            producer_version=MACRO_PRODUCER_VERSION,
        )


def test_identity_changes_when_closed_regime_changes() -> None:
    rising = _regime_state()
    falling = _regime_state(regimes=_bundle(rates_regime="falling"))
    assert rising.identity_tuple() != falling.identity_tuple()
    assert rising.to_dict()["schema_version"] == DRIVER_STATE_SCHEMA
    same = DriverState.from_mapping(rising.to_dict())
    assert same.identity_tuple() == rising.identity_tuple()


def test_raw_ids_and_text_are_rejected() -> None:
    legal = _regime_state().to_dict()
    for key, value in (
        ("observation_id", "macro_world_observation:v1:" + "a" * 64),
        ("fact_refs", ("macro_source_fact_version:v1:" + "b" * 64,)),
        ("metric_key", "policy_rate"),
        ("effective_until", VALID_UNTIL.isoformat()),
        ("brief_id", "company_micro:v1:brief"),
        ("symbol", "2330"),
        ("lei", "lei:549300"),
        ("driverKey", "oil"),
        ("text", "Fed hiked"),
    ):
        with pytest.raises(ValueError, match="raw identity|unknown fields|forbidden"):
            DriverState.from_mapping({**legal, key: value})
    with pytest.raises(ValueError, match="raw identity|unknown fields|forbidden"):
        DriverRegimeBundle.from_mapping({**_bundle().to_dict(), "observation_id": "macro_world_observation:v1:x"})
    with pytest.raises(ValueError, match="producer_version"):
        _regime_state(producer_version="world_macro_source.v0-unadmitted")
    with pytest.raises(FrozenInstanceError):
        rising = _regime_state()
        rising.signal_class = "missing"  # type: ignore[misc]


def test_world_driver_stays_stdlib_domain() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert "trader.reporting" not in source
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    tree = ast.parse(source, filename=str(MODULE_PATH))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if node.module.startswith("trader.") and not node.module.startswith("trader.domain"):
                violations.append(node.module)
            elif root != "trader" and root not in sys.stdlib_module_names:
                violations.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root != "trader" and root not in sys.stdlib_module_names:
                    violations.append(alias.name)
    assert violations == []


def _news_bundle(**overrides: object) -> DriverNewsBundle:
    values: dict[str, object] = {
        "event_class": "earnings",
        "event_class_source": "analyst",
        "direction": "bullish",
        "strength": "strong",
        "severity": "watch",
        "horizon_bucket": "quarters",
        "attribution_quality": "symbol_sourced",
    }
    values.update(overrides)
    return DriverNewsBundle(**values)  # type: ignore[arg-type]


def _company_bundle(**overrides: object) -> DriverCompanyBundle:
    values: dict[str, object] = {
        "sector": "eu_tech",
        "thesis_status": "intact",
        "coverage_status": "full",
        "freshness_status": "fresh",
        "catalyst_bucket": "few",
        "risk_bucket": "few",
        "depth": "screen",
    }
    values.update(overrides)
    return DriverCompanyBundle(**values)  # type: ignore[arg-type]


def _news_state(**overrides: object) -> DriverState:
    values: dict[str, object] = {
        "source_family": "knowledge_artifact",
        "signal_class": "news_state",
        "regimes": None,
        "news": _news_bundle(),
        "company": None,
        "artifact_kind": "news_macro",
        "until_bound": "bounded",
        "producer_version": NEWS_PRODUCER_VERSION,
        "transform_version": NEWS_TRANSFORM_VERSION,
        "missingness": "none",
    }
    values.update(overrides)
    return DriverState(**values)  # type: ignore[arg-type]


def _company_state(**overrides: object) -> DriverState:
    values: dict[str, object] = {
        "source_family": "knowledge_artifact",
        "signal_class": "company_state",
        "regimes": None,
        "news": None,
        "company": _company_bundle(),
        "artifact_kind": "company_intelligence",
        "until_bound": "bounded",
        "producer_version": COMPANY_PRODUCER_VERSION,
        "transform_version": COMPANY_TRANSFORM_VERSION,
        "missingness": "none",
    }
    values.update(overrides)
    return DriverState(**values)  # type: ignore[arg-type]


def test_news_state_round_trips_through_constructors_and_mapping() -> None:
    state = DriverState.from_news_signal(_news_bundle(), until_bound="bounded")
    assert state == _news_state()
    assert state.signal_class == "news_state"
    assert state.artifact_kind == "news_macro"
    replayed = DriverState.from_mapping(state.to_dict())
    assert replayed == state
    assert replayed.identity_tuple()[3] == (
        "earnings",
        "analyst",
        "bullish",
        "strong",
        "watch",
        "quarters",
        "symbol_sourced",
    )
    assert DriverState.from_mapping({**state.to_dict(), "news": _news_bundle().to_dict()}) == state


def test_company_state_round_trips_through_constructors_and_mapping() -> None:
    state = DriverState.from_company_signal(_company_bundle(), until_bound="unbounded")
    assert state.signal_class == "company_state"
    assert state.artifact_kind == "company_intelligence"
    replayed = DriverState.from_mapping(state.to_dict())
    assert replayed == state
    assert replayed.identity_tuple()[4] == ("eu_tech", "intact", "full", "fresh", "few", "few", "screen")


def test_news_state_matrix_rejects_crossed_bundles_and_versions() -> None:
    with pytest.raises(ValueError, match="requires a DriverNewsBundle"):
        _news_state(news=None)
    with pytest.raises(ValueError, match="cannot carry a regime or company bundle"):
        _news_state(company=_company_bundle())
    with pytest.raises(ValueError, match="source_family=knowledge_artifact"):
        _news_state(source_family="macro_observation")
    with pytest.raises(ValueError, match="artifact_kind=news_macro"):
        _news_state(artifact_kind="company_intelligence")
    with pytest.raises(ValueError, match="producer_version"):
        _news_state(producer_version="unspecified")
    with pytest.raises(ValueError, match="transform_version"):
        _news_state(transform_version="unspecified")
    with pytest.raises(ValueError, match="missingness=none"):
        _news_state(missingness="not_applicable")


def test_company_state_matrix_rejects_crossed_bundles_and_versions() -> None:
    with pytest.raises(ValueError, match="requires a DriverCompanyBundle"):
        _company_state(company=None)
    with pytest.raises(ValueError, match="cannot carry a regime or news bundle"):
        _company_state(news=_news_bundle())
    with pytest.raises(ValueError, match="source_family=knowledge_artifact"):
        _company_state(source_family="unproven")
    with pytest.raises(ValueError, match="artifact_kind=company_intelligence"):
        _company_state(artifact_kind="news_macro")
    with pytest.raises(ValueError, match="producer_version"):
        _company_state(producer_version=NEWS_PRODUCER_VERSION)
    with pytest.raises(ValueError, match="missingness=none"):
        _company_state(missingness="not_applicable")


def test_legacy_signal_classes_cannot_carry_news_or_company_bundles() -> None:
    with pytest.raises(ValueError, match="cannot carry a news or company bundle"):
        _regime_state(news=_news_bundle())
    missing = DriverState.missing(
        missingness="artifact_unjoined", source_family="knowledge_artifact", until_bound="unknown"
    )
    with pytest.raises(ValueError, match="cannot carry a news or company bundle"):
        DriverState.from_mapping({**missing.to_dict(), "company": _company_bundle().to_dict()})
    unspecified = DriverState.unspecified()
    with pytest.raises(ValueError, match="cannot carry a news or company bundle"):
        DriverState.from_mapping({**unspecified.to_dict(), "news": _news_bundle().to_dict()})
