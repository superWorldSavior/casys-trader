from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.situation import NewsMacroBrief
from trader.domain.world_context import NO_PROVEN_ARTIFACT_REASON, SCOPE_UNMAPPED_REASON
from trader.domain.world_macro import (
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
from trader.infrastructure.state_db.availability_receipt import (
    AVAILABILITY_RECEIPT_SCHEMA,
    payload_sha256,
)
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
from trader.infrastructure.state_db.world_context_reader import WorldContextReader
from trader.infrastructure.state_db.world_macro_store import WorldMacroStore


CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc)
BOOT = datetime(2026, 8, 23, 12, 30, tzinfo=timezone.utc)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=timezone.utc)
MAPPING_PATH = REPO_ROOT / "config" / "world_scope_mapping.yaml"


def _clock_at(moment: datetime):
    return lambda: moment


def _mapping():
    return WorldScopeResolver.load(MAPPING_PATH).mapping


def _news_brief(point: str = "ECB watch") -> NewsMacroBrief:
    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "2026-08-23|TW",
            "venue": "TW",
            "as_of": "2026-08-23T09:00:00+00:00",
            "valid_until": "2026-08-24T09:00:00+00:00",
            "zones": {"TW": [{"point": point, "sources": ["src-1"]}]},
        }
    )
    assert brief is not None
    return brief


def _company() -> CompanyIntelligenceBrief:
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": "AAA",
            "as_of": "2026-08-22T08:00:00+00:00",
            "input_signature": "sig-aaa",
            "depth": "screen",
            "issuer_identity": {"issuer_name": "AAA Ltd", "identity_status": "unverified"},
            "coverage": {"status": "partial"},
            "company_thesis": {"status": "intact"},
            "source_refs": ["src-a", "src-b"],
        }
    )
    assert brief is not None
    return brief


def _fact(**overrides: object) -> MacroSourceFact:
    values: dict[str, object] = {
        "fact_kind": "series_point",
        "metric_key": "policy_rate",
        "scope": MacroScope(kind="country", entity_id="iso-3166:TW"),
        "value": MacroNumericValue(number=4.25, unit="percent"),
        "period": "2026-08",
        "occurred_at": "2026-08-23T00:00:00Z",
        "published_at": "2026-08-23T11:30:00Z",
        "ingested_at": "2026-08-23T11:31:10Z",
        "source": MacroFactSource(
            provider_id="official_provider",
            adapter_version="official_provider.v1",
            source_record_id="stable-provider-id",
            source_ref="https://source.example/record",
        ),
        "valid_until": VALID_UNTIL,
    }
    values.update(overrides)
    return MacroSourceFact(**values)  # type: ignore[arg-type]


def _observation(*, fact: MacroSourceFact | None = None, **overrides: object) -> MacroWorldObservation:
    resolved = fact if fact is not None else _fact()
    fact_ref = resolved.fact_version_id.value
    values: dict[str, object] = {
        "scope": MacroScope(kind="venue", entity_id="mic:XTAI"),
        "cutoff_at": datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
        "producer_version": MACRO_PRODUCER_VERSION,
        "transform_version": MACRO_TRANSFORM_VERSION,
        "source_registry_version": MACRO_SOURCE_REGISTRY_VERSION,
        "fact_refs": (fact_ref,),
        "features": {"macro_regime": "mixed", "rates_regime": "stable", "usd_regime": "unknown"},
        "dimensions": (
            MacroDimensionState(
                dimension="macro_regime",
                value="mixed",
                coverage_status="complete",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(fact_ref,),
            ),
            MacroDimensionState(
                dimension="rates_regime",
                value="stable",
                coverage_status="complete",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(fact_ref,),
            ),
            MacroDimensionState(
                dimension="usd_regime",
                value="unknown",
                coverage_status="unknown",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(),
            ),
        ),
        "coverage": MacroCoverage(
            status="partial",
            required_sources=3,
            fresh_sources=2,
            missing_source_ids=("broad_usd_index",),
        ),
        "valid_until": VALID_UNTIL,
    }
    values.update(overrides)
    return MacroWorldObservation(**values)  # type: ignore[arg-type]


def _reader(
    tmp_path: Path,
    *,
    store: WorldMacroStore | None = None,
    clock=None,
    company_store: CompanyIntelligenceStore | None = None,
    news_store: NewsMacroBriefStore | None = None,
) -> WorldContextReader:
    return WorldContextReader(
        news_store=news_store,
        company_store=company_store,
        company_dir=None if company_store is not None else tmp_path / "company",
        macro_store=store,
        scope_mapping=_mapping(),
        clock=clock or _clock_at(BOOT),
    )


def test_public_append_does_not_accept_backdating_clocks() -> None:
    assert "recorded_at" not in inspect.signature(NewsMacroBriefStore.append).parameters
    assert "ready_at" not in inspect.signature(NewsMacroBriefStore.append).parameters
    assert "recorded_at" not in inspect.signature(CompanyIntelligenceStore.append).parameters
    assert "ready_at" not in inspect.signature(CompanyIntelligenceStore.append).parameters
    assert "ready_at" not in inspect.signature(WorldMacroStore.append_observation).parameters


def test_news_macro_brief_is_not_a_world_model_source(tmp_path: Path) -> None:
    news = NewsMacroBriefStore(tmp_path / "news_briefs", clock=lambda: READY)
    news.append(_news_brief())
    store = WorldMacroStore(tmp_path / "world_macro", clock=lambda: READY)
    reader = _reader(tmp_path, store=store, news_store=news, clock=_clock_at(BOOT))
    evidence = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
    assert evidence.proven is False
    assert evidence.payload is None or evidence.payload.get("features") in (None, {})
    source = Path("trader/infrastructure/state_db/world_context_reader.py").read_text(encoding="utf-8")
    assert "NewsMacroBrief" not in source
    assert "NewsMacroBriefStore" not in source
    assert "parse_macro_brief" not in source
    assert "gdelt" not in source.lower()
    assert news.read_latest("TW").venue == "TW"


def test_constructor_still_accepts_news_store_for_fail_open_daemon(tmp_path: Path) -> None:
    params = inspect.signature(WorldContextReader.__init__).parameters
    assert "news_store" in params
    assert "news_dir" in params
    reader = WorldContextReader(
        news_store=NewsMacroBriefStore(tmp_path / "news_briefs"),
        company_dir=tmp_path / "company",
        clock=_clock_at(BOOT),
    )
    evidence = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert evidence.status == "missing"


def test_legacy_observation_without_receipt_is_unproven(tmp_path: Path) -> None:
    root = tmp_path / "world_macro"
    history = root / "observations" / "2026-08-23.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps(_observation().to_dict(), sort_keys=True) + "\n", encoding="utf-8")
    reader = _reader(tmp_path, store=WorldMacroStore(root, clock=_clock_at(BOOT)))
    evidence = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
    assert evidence.proven is False
    assert evidence.artifact is None


def test_tampered_observation_digest_fails_closed(tmp_path: Path) -> None:
    store = WorldMacroStore(tmp_path / "world_macro", clock=lambda: READY)
    store.append_observation(_observation())
    history = tmp_path / "world_macro" / "observations" / "2026-08-23.jsonl"
    row = json.loads(history.read_text().splitlines()[0])
    row["features"]["macro_regime"] = "tightening"
    history.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    reader = _reader(tmp_path, store=WorldMacroStore(tmp_path / "world_macro", clock=_clock_at(BOOT)))
    evidence = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
    assert evidence.proven is False


def test_late_and_stale_macro_receipts_are_explicit(tmp_path: Path) -> None:
    late_store = WorldMacroStore(
        tmp_path / "late",
        clock=lambda: datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc),
    )
    late_store.append_observation(_observation())
    late_reader = _reader(
        tmp_path,
        store=late_store,
        clock=_clock_at(datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)),
    )
    late = late_reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert late.status == "missing"
    assert late.reason == NO_PROVEN_ARTIFACT_REASON
    assert late.proven is False
    later = late_reader.lookup_macro(
        venue="TW",
        symbol="2301.TW",
        cutoff_at=datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc),
    )
    assert later.status == "partial"
    assert later.proven is True
    assert later.artifact is not None
    assert later.payload is not None
    assert later.payload["features"]["usd_regime"] == "unknown"

    stale_store = WorldMacroStore(tmp_path / "stale", clock=lambda: READY)
    stale_store.append_observation(
        _observation(valid_until=datetime(2026, 8, 23, 12, 30, tzinfo=timezone.utc))
    )
    stale = _reader(tmp_path, store=stale_store, clock=_clock_at(BOOT)).lookup_macro(
        venue="TW",
        symbol="2301.TW",
        cutoff_at=CUTOFF,
    )
    assert stale.status == "stale"
    assert stale.proven is True
    assert stale.artifact is not None
    assert stale.artifact.valid_until is not None
    assert stale.artifact.valid_until <= CUTOFF


def test_reader_never_uses_as_of_or_mtime_as_readiness(tmp_path: Path) -> None:
    root = tmp_path / "world_macro"
    history = root / "observations" / "2026-08-23.jsonl"
    history.parent.mkdir(parents=True)
    payload = _observation().to_dict()
    payload["cutoff_at"] = "2026-08-21T00:00:00+00:00"
    history.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    reader = _reader(tmp_path, store=WorldMacroStore(root, clock=_clock_at(BOOT)))
    evidence = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
    assert evidence.proven is False


def test_eligible_company_receipt_is_complete(tmp_path: Path) -> None:
    companies = CompanyIntelligenceStore(
        tmp_path / "company_intelligence",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    companies.append(_company())
    reader = WorldContextReader(
        news_dir=tmp_path / "news",
        company_store=companies,
        clock=_clock_at(datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc)),
    )
    evidence = reader.lookup_company(symbol="AAA", cutoff_at=datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc))
    assert evidence.status == "complete"
    assert evidence.artifact is not None
    assert evidence.artifact.ready_at <= datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc)


def test_nested_history_envelope_is_not_readiness_proof(tmp_path: Path) -> None:
    root = tmp_path / "world_macro"
    history = root / "observations" / "2026-08-23.jsonl"
    history.parent.mkdir(parents=True)
    payload = _observation().to_dict()
    envelope = {
        "schema_version": AVAILABILITY_RECEIPT_SCHEMA,
        "ready_at": "2026-08-23T12:00:00+00:00",
        "payload_sha256": payload_sha256(payload),
        "payload": payload,
    }
    history.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    reader = _reader(tmp_path, store=WorldMacroStore(root, clock=_clock_at(BOOT)))
    nested = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert nested.status == "missing"
    assert nested.reason == NO_PROVEN_ARTIFACT_REASON


def test_missing_late_and_unproven_share_canonical_evidence(tmp_path: Path) -> None:
    empty = _reader(tmp_path, store=WorldMacroStore(tmp_path / "empty", clock=_clock_at(BOOT)))
    missing = empty.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)

    late_store = WorldMacroStore(
        tmp_path / "late_macro",
        clock=lambda: datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc),
    )
    late_store.append_observation(_observation())
    late = _reader(
        tmp_path,
        store=late_store,
        clock=_clock_at(datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)),
    ).lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)

    legacy_root = tmp_path / "legacy"
    history = legacy_root / "observations" / "2026-08-23.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps(_observation().to_dict(), sort_keys=True) + "\n", encoding="utf-8")
    unproven = _reader(tmp_path, store=WorldMacroStore(legacy_root, clock=_clock_at(BOOT))).lookup_macro(
        venue="TW",
        symbol="2301.TW",
        cutoff_at=CUTOFF,
    )

    for evidence in (missing, late, unproven):
        assert evidence.status == "missing"
        assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
        assert evidence.proven is False
        assert evidence.artifact is None


def test_receipt_declared_before_cutoff_but_first_seen_after_stays_missing(tmp_path: Path) -> None:
    clock = {"now": datetime(2026, 8, 23, 11, 0, tzinfo=timezone.utc)}
    store = WorldMacroStore(tmp_path / "world_macro", clock=lambda: datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc))
    reader = _reader(tmp_path, store=store, clock=lambda: clock["now"])
    before = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert before.status == "missing"

    store.append_observation(_observation())
    clock["now"] = datetime(2026, 8, 23, 13, 30, tzinfo=timezone.utc)
    after = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert after.status == "missing"
    assert after.reason == NO_PROVEN_ARTIFACT_REASON
    assert after.proven is False
    assert after.artifact is None


def test_restart_applies_conservative_first_seen(tmp_path: Path) -> None:
    writer = WorldMacroStore(tmp_path / "world_macro", clock=lambda: READY)
    writer.append_observation(_observation())
    restarted = WorldMacroStore(tmp_path / "world_macro", clock=_clock_at(datetime(2026, 8, 23, 13, 30, tzinfo=timezone.utc)))
    reader = _reader(
        tmp_path,
        store=restarted,
        clock=_clock_at(datetime(2026, 8, 23, 13, 30, tzinfo=timezone.utc)),
    )
    too_early = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert too_early.status == "missing"
    assert too_early.reason == NO_PROVEN_ARTIFACT_REASON
    later = reader.lookup_macro(
        venue="TW",
        symbol="2301.TW",
        cutoff_at=datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc),
    )
    assert later.status == "partial"
    assert later.proven is True
    assert later.artifact is not None
    assert later.artifact.ready_at == datetime(2026, 8, 23, 13, 30, tzinfo=timezone.utc)


def test_receipt_primed_before_later_cutoff_becomes_eligible(tmp_path: Path) -> None:
    store = WorldMacroStore(tmp_path / "world_macro", clock=lambda: READY)
    store.append_observation(_observation())
    reader = _reader(tmp_path, store=store, clock=_clock_at(BOOT))
    evidence = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert evidence.status == "partial"
    assert evidence.proven is True
    assert evidence.artifact is not None
    assert evidence.artifact.kind == "macro_world_observation"
    assert evidence.artifact.ready_at == BOOT
    assert evidence.payload is not None
    assert evidence.payload["features"]["usd_regime"] == "unknown"
    assert evidence.payload["scope_resolution"]["status"] == "resolved" or evidence.payload["scope_resolution"][
        "resolution_status"
    ] == "resolved"


def _fact_with_record(record_id: str) -> MacroSourceFact:
    return _fact(
        source=MacroFactSource(
            provider_id="official_provider",
            adapter_version="official_provider.v1",
            source_record_id=record_id,
            source_ref="https://source.example/record",
        )
    )


def test_two_suffix_selects_exact_xtai_scope_only(tmp_path: Path) -> None:
    store = WorldMacroStore(tmp_path / "world_macro", clock=lambda: READY)
    store.append_observation(_observation())
    eu_fact = _fact_with_record("eu-rate")
    other = _observation(
        fact=eu_fact,
        scope=MacroScope(kind="venue", entity_id="mic:XPAR"),
        features={"macro_regime": "tightening", "rates_regime": "rising", "usd_regime": "unknown"},
        dimensions=(
            MacroDimensionState(
                dimension="macro_regime",
                value="tightening",
                coverage_status="complete",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(eu_fact.fact_version_id.value,),
            ),
            MacroDimensionState(
                dimension="rates_regime",
                value="rising",
                coverage_status="complete",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(eu_fact.fact_version_id.value,),
            ),
            MacroDimensionState(
                dimension="usd_regime",
                value="unknown",
                coverage_status="unknown",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(),
            ),
        ),
    )
    store.append_observation(other)
    tw_fact = _fact_with_record("tw-cpi")
    country = _observation(
        fact=tw_fact,
        scope=MacroScope(kind="country", entity_id="iso-3166:TW"),
        features={"macro_regime": "easing", "rates_regime": "falling", "usd_regime": "unknown"},
        dimensions=(
            MacroDimensionState(
                dimension="macro_regime",
                value="easing",
                coverage_status="complete",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(tw_fact.fact_version_id.value,),
            ),
            MacroDimensionState(
                dimension="rates_regime",
                value="falling",
                coverage_status="complete",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(tw_fact.fact_version_id.value,),
            ),
            MacroDimensionState(
                dimension="usd_regime",
                value="unknown",
                coverage_status="unknown",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(),
            ),
        ),
    )
    store.append_observation(country)
    reader = _reader(tmp_path, store=store, clock=_clock_at(BOOT))
    evidence = reader.lookup_macro(venue="TW", symbol="6488.TWO", cutoff_at=CUTOFF)
    assert evidence.status == "partial"
    assert evidence.payload is not None
    assert evidence.payload["features"]["macro_regime"] == "mixed"
    assert evidence.artifact is not None
    assert evidence.artifact.subjects[0].entity_id == "mic:XTAI"


def test_unmapped_gm_keeps_mapping_identity_and_does_not_fallback(tmp_path: Path) -> None:
    store = WorldMacroStore(tmp_path / "world_macro", clock=lambda: READY)
    store.append_observation(_observation())
    reader = _reader(tmp_path, store=store, clock=_clock_at(BOOT))
    evidence = reader.lookup_macro(venue="GM", symbol="BMW.DE", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == SCOPE_UNMAPPED_REASON
    assert evidence.proven is False
    assert evidence.artifact is None
    assert evidence.payload is not None
    resolution = evidence.payload["scope_resolution"]
    assert resolution["status"] == "unmapped" or resolution["resolution_status"] == "unmapped"
    assert resolution["mapping_id"] == "world_scope_mapping.v1"
    assert len(resolution["mapping_sha256"]) == 64


def test_unknown_usd_stays_unknown_on_partial_observation(tmp_path: Path) -> None:
    store = WorldMacroStore(tmp_path / "world_macro", clock=lambda: READY)
    store.append_observation(_observation())
    reader = _reader(tmp_path, store=store, clock=_clock_at(BOOT))
    evidence = reader.lookup_macro(venue="TW", symbol="2301.TW", cutoff_at=CUTOFF)
    assert evidence.status == "partial"
    assert evidence.payload is not None
    assert evidence.payload["features"]["usd_regime"] == "unknown"
    assert evidence.payload["features"]["macro_regime"] == "mixed"


def test_lookup_macro_requires_instrument_and_has_no_mic_heuristic() -> None:
    params = inspect.signature(WorldContextReader.lookup_macro).parameters
    assert "symbol" in params
    assert "venue" in params
    source = Path("trader/infrastructure/state_db/world_context_reader.py").read_text(encoding="utf-8")
    lowered = source.lower()
    assert "xnys" not in lowered
    assert "xnas" not in lowered
    assert "tw ->" not in lowered
    assert "us ->" not in lowered
    assert "eu ->" not in lowered
