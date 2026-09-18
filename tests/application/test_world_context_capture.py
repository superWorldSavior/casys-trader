from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pathlib import Path

from trader.application.world_model.capture import capture_world_episodes
from trader.application.world_model.context_capture import attach_world_context, build_world_context_snapshot
from trader.application.world_model.encoding import FEATURE_CONTRACT_FINGERPRINT
from trader.domain.world_context import (
    CONTEXT_FEATURE_CONTRACT_ID,
    NO_PROVEN_ARTIFACT_REASON,
    SCOPE_UNMAPPED_REASON,
    EntityRef,
    KnowledgeArtifact,
    SensorEvidence,
)
from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_ID, WorldEpisode


CUTOFF = datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc)
FROZEN_V1_FINGERPRINT = "4f00c35f1989b02ac504e9cf2c102c9b045f3eff74da22b5a4d31fb0e03738b6"
V1_EPISODE_ID = "world-episode:v1:a228bc3d0bffc20d69bacda2edd133c902819de56427afece301a01af3d80809"


class _FakeSource:
    def __init__(self, macro: SensorEvidence, company: SensorEvidence) -> None:
        self.macro = macro
        self.company = company
        self.macro_calls: list[tuple[str, str]] = []

    def lookup_macro(self, *, venue: str, cutoff_at, symbol: str) -> SensorEvidence:
        self.macro_calls.append((venue, symbol))
        return self.macro

    def lookup_company(self, *, symbol: str, cutoff_at) -> SensorEvidence:
        return self.company


def _market_episode() -> WorldEpisode:
    return capture_world_episodes(
        active_symbols=("AAA",),
        tradable_symbols=("AAA",),
        bars_by_symbol={
            "AAA": [
                {
                    "ts": "2026-08-22T10:00:00+00:00",
                    "open": 100.0,
                    "high": 104.0,
                    "low": 99.0,
                    "close": 102.0,
                    "volume": 1000.0,
                    "available_at": "2026-08-22T10:05:00+00:00",
                    "source": "unit-market-bars",
                    "interval": "1h",
                    "timestamp_semantics": "bar_close",
                }
            ]
        },
        market_metadata_by_symbol={
            "AAA": {
                "venue": "XTAI",
                "asset_family": "equity",
                "session_phase": "regular",
                "market_regime": "trending_up",
                "family_regime": "risk_on",
                "freshness": {"status": "fresh", "data_age_minutes": 5.0},
                "available_at": "2026-08-22T10:05:00+00:00",
            }
        },
        source="unit-market-bars",
        interval="1h",
        timestamp_semantics="bar_close",
        captured_at="2026-08-22T10:30:00+00:00",
    )[0]


def test_market_fingerprint_and_capture_identity_remain_frozen() -> None:
    episode = _market_episode()
    assert FEATURE_CONTRACT_FINGERPRINT == FROZEN_V1_FINGERPRINT
    assert episode.observation.feature_contract_version == MARKET_FEATURE_CONTRACT_ID
    assert episode.episode_id == V1_EPISODE_ID
    assert "context" not in episode.observation.to_dict()


def test_missing_and_late_context_still_emit_trainable_context_episodes() -> None:
    v1 = _market_episode()
    source = _FakeSource(
        SensorEvidence(status="late", reason="ready_at_after_cutoff", proven=True),
        SensorEvidence(status="missing", reason="no_artifact"),
    )
    v2 = attach_world_context((v1,), source)[0]
    assert v2.observation.feature_contract_version == CONTEXT_FEATURE_CONTRACT_ID
    assert v2.episode_id != v1.episode_id
    assert v2.training_eligible is True
    assert v2.observation.context["status"] == "missing"
    assert v2.observation.context["categorical_features"]["macro_status"] == "missing"
    assert v2.observation.context["categorical_features"]["company_status"] == "missing"
    assert v1.episode_id == V1_EPISODE_ID


def test_unproven_context_is_explicit_and_does_not_drop_the_market_episode() -> None:
    v1 = _market_episode()
    source = _FakeSource(
        SensorEvidence(status="availability_unproven", reason="legacy_row_without_receipt"),
        SensorEvidence(status="availability_unproven", reason="legacy_row_without_receipt"),
    )
    v2 = attach_world_context((v1,), source)[0]
    assert v2.observation.context["status"] == "missing"
    assert v2.training_eligible is True
    assert v2.observation.context["categorical_features"]["context_status"] == "missing"
    assert v2.observation.context["categorical_features"]["macro_status"] == "missing"


def test_missing_late_and_unproven_share_context_identity() -> None:
    v1 = _market_episode()
    missing = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    late = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="late", reason="ready_at_after_cutoff", proven=True),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    unproven = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="availability_unproven", reason="legacy_row_without_receipt"),
            SensorEvidence(status="availability_unproven", reason="legacy_row_without_receipt"),
        ),
    )[0]
    assert missing.episode_id == late.episode_id == unproven.episode_id
    assert missing.observation.context["context_id"] == late.observation.context["context_id"]
    assert {item.get("reason") for item in missing.observation.context["artifact_proofs"]} == {
        NO_PROVEN_ARTIFACT_REASON
    }


def test_context_cutoff_is_completed_bar_clock_not_poll_time() -> None:
    v1 = _market_episode()
    later = v1.observation
    # Poll clocks can move; the V1 available_at stays the observation market time.
    assert later.available_at is not None
    source = _FakeSource(
        SensorEvidence(status="missing", reason="no_artifact"),
        SensorEvidence(status="missing", reason="no_artifact"),
    )
    first = attach_world_context((v1,), source)[0]
    second = attach_world_context((v1,), source)[0]
    assert first.episode_id == second.episode_id
    assert first.observation.context["cutoff_at"] == "2026-08-22T10:00:00+00:00"
    assert first.observation.available_at == v1.observation.available_at


def test_unknown_timestamp_semantics_fail_closed_without_v2() -> None:
    from trader.domain.world_episode import AnchorBar, WorldEpisode, WorldObservation

    observation = WorldObservation(
        venue="XTAI",
        symbol="AAA",
        bar_interval="1h",
        as_of_bar_ts="2026-08-22T10:00:00+00:00",
        feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
        sampling_policy_version="active_tradable_completed_bar.v1",
        anchor=AnchorBar(
            ts="2026-08-22T10:00:00+00:00",
            open=100.0,
            high=104.0,
            low=99.0,
            close=102.0,
            volume=1000.0,
            source="unit-market-bars",
            timestamp_semantics="unknown",
        ),
        available_at="2026-08-22T10:05:00+00:00",
        captured_at="2026-08-22T10:30:00+00:00",
        freshness={"status": "fresh", "data_age_minutes": 5.0},
        categorical_features={"venue": "XTAI"},
        numeric_features={"return": 0.01},
    )
    episode = WorldEpisode(observation=observation)
    source = _FakeSource(
        SensorEvidence(status="missing", reason="no_artifact"),
        SensorEvidence(status="missing", reason="no_artifact"),
    )
    assert attach_world_context((episode,), source) == ()


def test_proven_company_issuer_uses_artifact_ready_at(tmp_path) -> None:
    from trader.domain.company import CompanyIntelligenceBrief
    from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
    from trader.infrastructure.state_db.world_context_reader import WorldContextReader

    ready = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)
    companies = CompanyIntelligenceStore(tmp_path / "company_intelligence", clock=lambda: ready)
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": "AAA",
            "as_of": "2026-08-22T08:00:00+00:00",
            "input_signature": "sig-aaa",
            "depth": "screen",
            "issuer_identity": {
                "issuer_name": "AAA Ltd",
                "identity_status": "verified",
                "external_ids": {"lei": "5493001KJTIIGC8Y1R12"},
            },
            "coverage": {"status": "partial"},
            "company_thesis": {"status": "intact"},
            "source_refs": ["src-a"],
        }
    )
    assert brief is not None
    companies.append(brief)
    reader = WorldContextReader(
        news_dir=tmp_path / "news",
        company_store=companies,
        clock=lambda: ready,
    )
    v2 = attach_world_context((_market_episode(),), reader)[0]
    issued = [edge for edge in v2.observation.context["topology_edges"] if edge["kind"] == "ISSUED_BY"]
    assert len(issued) == 1
    assert issued[0]["ready_at"] == ready.isoformat()
    assert list(issued[0]["source_refs"]) == ["src-a"]
    assert "semantic_catalog" not in issued[0]["source_refs"]
    assert issued[0]["target"]["entity_id"] == "lei:5493001KJTIIGC8Y1R12"


def test_future_company_receipt_does_not_backdate_issuer(tmp_path) -> None:
    from trader.domain.company import CompanyIntelligenceBrief
    from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
    from trader.infrastructure.state_db.world_context_reader import WorldContextReader

    companies = CompanyIntelligenceStore(
        tmp_path / "company_intelligence",
        clock=lambda: datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc),
    )
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": "AAA",
            "as_of": "2026-08-22T08:00:00+00:00",
            "input_signature": "sig-aaa",
            "depth": "screen",
            "issuer_identity": {
                "issuer_name": "AAA Ltd",
                "identity_status": "verified",
                "external_ids": {"lei": "5493001KJTIIGC8Y1R12"},
            },
            "coverage": {"status": "partial"},
            "company_thesis": {"status": "intact"},
            "source_refs": ["src-a"],
        }
    )
    assert brief is not None
    companies.append(brief)
    reader = WorldContextReader(
        news_dir=tmp_path / "news",
        company_store=companies,
        clock=lambda: datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc),
    )
    v2 = attach_world_context((_market_episode(),), reader)[0]
    edges = v2.observation.context["topology_edges"]
    assert all(edge["kind"] != "ISSUED_BY" for edge in edges)
    assert all(edge["target"]["kind"] != "company" for edge in edges)


def test_post_cutoff_poll_does_not_mint_a_second_context_episode(tmp_path) -> None:
    from trader.domain.company import CompanyIntelligenceBrief
    from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
    from trader.infrastructure.state_db.world_context_reader import WorldContextReader
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    v1 = _market_episode()
    companies = CompanyIntelligenceStore(tmp_path / "company_intelligence")
    reader = WorldContextReader(news_dir=tmp_path / "news", company_store=companies)
    first = attach_world_context((v1,), reader)[0]
    store = WorldModelStore(tmp_path / "world_model.db")
    assert store.append_episode(first) is True

    later = CompanyIntelligenceStore(
        tmp_path / "company_intelligence",
        clock=lambda: datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc),
    )
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": "AAA",
            "as_of": "2026-08-22T08:00:00+00:00",
            "input_signature": "sig-late",
            "depth": "screen",
            "issuer_identity": {"issuer_name": "AAA Ltd", "identity_status": "unverified"},
            "coverage": {"status": "partial"},
            "company_thesis": {"status": "intact"},
            "source_refs": ["src-late"],
        }
    )
    assert brief is not None
    later.append(brief)
    second_reader = WorldContextReader(news_dir=tmp_path / "news", company_store=later)
    second = attach_world_context((v1,), second_reader)[0]
    assert second.episode_id == first.episode_id
    assert second.observation.context["context_id"] == first.observation.context["context_id"]
    assert store.append_episode(second) is False
    assert store.counts()["episodes"] == 1


def test_late_receipt_between_same_bar_polls_does_not_change_context_identity(tmp_path) -> None:
    from trader.domain.company import CompanyIntelligenceBrief
    from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
    from trader.infrastructure.state_db.world_context_reader import WorldContextReader

    clock = {"now": datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)}
    companies = CompanyIntelligenceStore(
        tmp_path / "company_intelligence",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    reader = WorldContextReader(
        news_dir=tmp_path / "news",
        company_store=companies,
        clock=lambda: clock["now"],
    )
    v1 = _market_episode()
    first = attach_world_context((v1,), reader)[0]
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": "AAA",
            "as_of": "2026-08-22T08:00:00+00:00",
            "input_signature": "sig-late-same-bar",
            "depth": "screen",
            "issuer_identity": {"issuer_name": "AAA Ltd", "identity_status": "unverified"},
            "coverage": {"status": "partial"},
            "company_thesis": {"status": "intact"},
            "source_refs": ["src-late"],
        }
    )
    assert brief is not None
    companies.append(brief)
    clock["now"] = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)
    second = attach_world_context((v1,), reader)[0]
    assert first.episode_id == second.episode_id
    assert first.observation.context["context_id"] == second.observation.context["context_id"]
    assert first.observation.context["status"] == "missing"
    assert second.observation.context["status"] == "missing"


def _missing_source() -> _FakeSource:
    return _FakeSource(
        SensorEvidence(status="missing", reason="no_artifact"),
        SensorEvidence(status="missing", reason="no_artifact"),
    )


def test_frozen_market_asset_family_is_not_reminted_from_catalog_or_injected_lookup(monkeypatch) -> None:
    import inspect
    from pathlib import Path

    from trader.domain.semantic import catalog

    monkeypatch.setattr(catalog, "family_for_symbol", lambda _symbol: "crypto")
    v1 = _market_episode()
    assert v1.observation.categorical_features["asset_family"] == "equity"
    v2 = attach_world_context((v1,), _missing_source())[0]
    families = [edge for edge in v2.observation.context["topology_edges"] if edge["kind"] == "MEMBER_OF_FAMILY"]
    assert len(families) == 1
    assert families[0]["target"]["entity_id"] == "equity"
    assert v1.episode_id == V1_EPISODE_ID
    assert "family_lookup" not in inspect.signature(attach_world_context).parameters
    from trader.runtime.world_model_runtime import WorldContextEpisodeEnricher

    assert "family_lookup" not in inspect.signature(WorldContextEpisodeEnricher.__init__).parameters
    source = Path(__file__).resolve().parents[2] / "trader" / "application" / "world_model" / "context_capture.py"
    text = source.read_text(encoding="utf-8")
    assert "family_for_symbol" not in text
    assert "FamilyLookup" not in text
    with pytest.raises(TypeError):
        attach_world_context((v1,), _missing_source(), family_lookup=lambda _symbol: "crypto")


def test_attach_passes_instrument_into_macro_lookup() -> None:
    source = _missing_source()
    attach_world_context((_market_episode(),), source)
    assert source.macro_calls == [("XTAI", "AAA")]


def test_context_macro_sensor_is_source_only_observation_not_news_brief() -> None:
    v2 = attach_world_context((_market_episode(),), _missing_source())[0]
    kinds = {item["kind"] for item in v2.observation.context["artifact_proofs"]}
    statuses = v2.observation.context["sensor_statuses"]
    assert "macro_world_observation" in kinds
    assert "macro_world_observation" in statuses
    assert "news_macro" not in kinds
    assert "news_macro" not in statuses
    capture_source = (
        Path(__file__).resolve().parents[2] / "trader" / "application" / "world_model" / "context_capture.py"
    ).read_text(encoding="utf-8")
    assert "NewsMacroBrief" not in capture_source
    assert "'news_macro'" not in capture_source
    assert '"news_macro"' not in capture_source


def test_partial_macro_observation_extracts_proven_dimensions_and_keeps_unknown_usd() -> None:
    ready = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)
    artifact = KnowledgeArtifact(
        kind="macro_world_observation",
        artifact_id="macro_world_observation:v1:" + "a" * 64,
        subjects=[EntityRef("venue", "mic:XTAI")],
        schema_version="macro_world_observation.v1",
        content_sha256="abc",
        ready_at=ready,
        valid_until=datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
    )
    snapshot = build_world_context_snapshot(
        symbol="2301.TW",
        venue="TW",
        cutoff_at=CUTOFF,
        family="equity",
        macro=SensorEvidence(
            status="partial",
            reason="sidecar_ready",
            proven=True,
            artifact=artifact,
            payload={
                "features": {"macro_regime": "mixed", "rates_regime": "stable", "usd_regime": "unknown"},
                "scope_resolution": {
                    "mapping_id": "world_scope_mapping.v1",
                    "mapping_sha256": "b" * 64,
                    "resolution_status": "resolved",
                    "anchor": {"market_venue": "TW", "instrument": "2301.TW"},
                },
            },
        ),
        company=SensorEvidence(status="missing", reason="no_artifact"),
    )
    features = snapshot.categorical_features
    assert features["macro_status"] == "partial"
    assert features["context_macro_regime"] == "mixed"
    assert features["context_rates_regime"] == "stable"
    assert features["context_usd_regime"] == "unknown"
    assert snapshot.sensor_statuses["macro_world_observation"] == "partial"
    assert snapshot.status == "partial"
    proofs = [item for item in snapshot.artifact_proofs if item["kind"] == "macro_world_observation"]
    assert proofs[0]["mapping_id"] == "world_scope_mapping.v1"
    assert proofs[0]["resolution_status"] == "resolved"


def test_described_by_uses_honest_country_subject_not_logical_venue() -> None:
    ready = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)
    artifact = KnowledgeArtifact(
        kind="macro_world_observation",
        artifact_id="macro_world_observation:v1:" + "b" * 64,
        subjects=[EntityRef("country", "iso-3166:US")],
        schema_version="macro_world_observation.v1",
        content_sha256="abc",
        ready_at=ready,
        valid_until=datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
    )
    snapshot = build_world_context_snapshot(
        symbol="GM",
        venue="US",
        cutoff_at=CUTOFF,
        family="equity",
        macro=SensorEvidence(
            status="partial",
            reason="sidecar_ready",
            proven=True,
            artifact=artifact,
            payload={
                "features": {"macro_regime": "mixed", "rates_regime": "stable", "usd_regime": "unknown"},
                "origin_scope": {"kind": "country", "entity_id": "iso-3166:US"},
                "ancestry_distance": 1,
                "producer_version": "world_macro_source.v1",
                "scope_resolution": {
                    "mapping_id": "world_scope_mapping.v1",
                    "mapping_sha256": "b" * 64,
                    "resolution_status": "resolved",
                    "anchor": {"market_venue": "US", "instrument": "GM"},
                },
            },
        ),
        company=SensorEvidence(status="missing", reason="no_artifact"),
    )
    described = [edge for edge in snapshot.topology_edges if edge.kind == "DESCRIBED_BY"]
    macro_edges = [edge for edge in described if edge.target.entity_id == "macro_world_observation"]
    assert len(macro_edges) == 1
    assert macro_edges[0].source.kind == "country"
    assert macro_edges[0].source.entity_id == "iso-3166:US"
    assert not any(
        edge.kind == "DESCRIBED_BY" and edge.source.kind == "venue" and edge.source.entity_id == "US"
        for edge in snapshot.topology_edges
    )
    proofs = [item for item in snapshot.artifact_proofs if item["kind"] == "macro_world_observation"]
    assert proofs[0]["origin_scope"] == {"kind": "country", "entity_id": "iso-3166:US"}
    assert proofs[0]["ancestry_distance"] == 1
    assert snapshot.categorical_features["context_macro_regime"] == "mixed"


def test_unmapped_gm_stays_missing_with_explicit_resolution_status() -> None:
    snapshot = build_world_context_snapshot(
        symbol="BMW.DE",
        venue="GM",
        cutoff_at=CUTOFF,
        family="equity",
        macro=SensorEvidence(
            status="missing",
            reason=SCOPE_UNMAPPED_REASON,
            proven=False,
            payload={
                "scope_resolution": {
                    "mapping_id": "world_scope_mapping.v1",
                    "mapping_sha256": "b" * 64,
                    "resolution_status": "unmapped",
                    "anchor": {"market_venue": "GM", "instrument": "BMW.DE"},
                }
            },
        ),
        company=SensorEvidence(status="missing", reason="no_artifact"),
    )
    assert snapshot.categorical_features["macro_status"] == "missing"
    assert snapshot.categorical_features["context_usd_regime"] == "missing"
    proof = next(item for item in snapshot.artifact_proofs if item["kind"] == "macro_world_observation")
    assert proof["reason"] == SCOPE_UNMAPPED_REASON
    assert proof["resolution_status"] == "unmapped"
    assert proof["mapping_id"] == "world_scope_mapping.v1"
    assert snapshot.artifact_refs == ()


def test_absent_frozen_asset_family_omits_member_of_family_edge() -> None:
    from trader.domain.world_episode import AnchorBar, WorldEpisode, WorldObservation

    observation = WorldObservation(
        venue="US",
        symbol="AAPL",
        bar_interval="1h",
        as_of_bar_ts="2026-08-22T10:00:00+00:00",
        feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
        sampling_policy_version="active_tradable_completed_bar.v1",
        anchor=AnchorBar(
            ts="2026-08-22T10:00:00+00:00",
            open=100.0,
            high=104.0,
            low=99.0,
            close=102.0,
            volume=1000.0,
            source="unit-market-bars",
            timestamp_semantics="bar_close",
        ),
        available_at="2026-08-22T10:05:00+00:00",
        captured_at="2026-08-22T10:30:00+00:00",
        freshness={"status": "fresh", "data_age_minutes": 5.0},
        categorical_features={"venue": "US"},
        numeric_features={"return": 0.01},
    )
    v2 = attach_world_context((WorldEpisode(observation=observation),), _missing_source())[0]
    edges = v2.observation.context["topology_edges"]
    assert all(edge["kind"] != "MEMBER_OF_FAMILY" for edge in edges)
    assert all(edge["target"]["kind"] != "family" for edge in edges)
