from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

from tests.application.test_world_context_capture import V1_EPISODE_ID, _FakeSource, _v1_episode
from tests.application.test_world_graph_snapshot import (
    _InMemorySnapshotLedger,
    _InMemoryWorldGraphLedger,
    _episode as _graph6_v1_episode,
    _mapping,
    _seed_rfc_graph,
    _service,
)
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.context_capture import attach_world_context
from trader.application.world_model.graph_snapshot import WorldGraphSnapshotService
from trader.domain.world_context import SensorEvidence
from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_VERSION, WorldEpisode
from trader.domain.world_feature_contract import (
    GRAPH_CONTENT_CATEGORICAL_FEATURES,
    GRAPH_FEATURE_CONTRACT_VERSION,
    GRAPH_STATUS_CATEGORICAL_FEATURES,
    WORLD_GRAPH_V3_CONFIG_SHA256,
    world_v3_graph_content_mask,
    world_v3_topology_status_only_mask,
)
from trader.domain.world_scope import WorldMarketAnchorRef, WorldScopeMapping, WorldScopeResolution


_GRAPH_CAPTURE = REPO_ROOT / "trader" / "application" / "world_model" / "graph_capture.py"
_FORBIDDEN_IMPORT_PREFIXES = (
    "networkx",
    "httpx",
    "openai",
    "anthropic",
    "requests",
    "urllib.request",
    "trader.infrastructure",
    "trader.runtime",
    "trader.reporting",
    "yaml",
)


def _import_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel_path = path.relative_to(REPO_ROOT)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"{rel_path}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{rel_path}: import {alias.name}")
    source = path.read_text(encoding="utf-8")
    for marker in ("world_graph_store", "world_temporal_networkx", "world_graph_v3.yaml"):
        if marker in source:
            violations.append(f"{rel_path}: {marker} mentioned")
    return violations


def _capture_config(ledger=None, mapping=None, **overrides):
    from trader.application.world_model.graph_capture import WorldGraphCaptureConfig

    resolved_mapping = _mapping() if mapping is None else mapping
    resolved_ledger = _InMemoryWorldGraphLedger() if ledger is None else ledger
    values = {
        "scope_mapping": resolved_mapping,
        "snapshot_service": _service(resolved_ledger),
        "study_cohort_id": None,
    }
    values.update(overrides)
    return WorldGraphCaptureConfig(**values)


def _complete_config(**overrides):
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    return _capture_config(ledger=ledger, mapping=mapping, **overrides)


def _unpublished_config(**overrides):
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    return _capture_config(ledger=ledger, mapping=mapping, **overrides)


def _empty_mapping() -> WorldScopeMapping:
    return WorldScopeMapping(mapping_id="world_scope_mapping.v1", entries=())


def _unmapped_config(**overrides):
    from trader.application.world_model.ontology_service import (
        PublishWorldOntologyRevision,
        WorldOntologyService,
    )
    from tests.application.test_world_graph_snapshot import _revision_for

    mapping = _empty_mapping()
    ledger = _InMemoryWorldGraphLedger(now=datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc))
    WorldOntologyService(ledger).publish_revision(
        PublishWorldOntologyRevision(revision=_revision_for(mapping, entities=(), structural=(), identity_links=()))
    )
    return _capture_config(ledger=ledger, mapping=mapping, **overrides)


def _attach(episodes, config):
    from trader.application.world_model.graph_capture import attach_world_graph

    return attach_world_graph(episodes, config)


def _graph_features(observation) -> dict[str, object]:
    raw = observation.graph_features
    if raw is None:
        payload = observation.to_dict().get("graph_features") or {}
        return dict(payload) if isinstance(payload, dict) else {}
    if hasattr(raw, "keys"):
        return dict(raw)
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _graph_snapshot(observation):
    graph = getattr(observation, "graph", None)
    if graph is not None:
        return graph
    features = _graph_features(observation)
    snapshot = features.get("snapshot")
    if snapshot is None:
        return None
    from trader.domain.world_graph import WorldGraphSnapshot

    return snapshot if hasattr(snapshot, "snapshot_id") else WorldGraphSnapshot.from_mapping(snapshot)


def test_graph_capture_does_not_import_store_runtime_or_yaml_config() -> None:
    assert _GRAPH_CAPTURE.exists()
    assert _import_violations(_GRAPH_CAPTURE) == []
    source = _GRAPH_CAPTURE.read_text(encoding="utf-8")
    assert "study_cohort_id" in source
    assert WORLD_GRAPH_V3_CONFIG_SHA256 == "e8d1df0df1badfdd9c2004ae4cf9eacb9605347abacbd63563a844dcfe16ab84"


def test_v1_identity_stays_frozen_and_v3_companion_shares_the_market_slot() -> None:
    v1 = _v1_episode()
    v3 = _attach((v1,), _unmapped_config())[0]
    assert v1.episode_id == V1_EPISODE_ID
    assert v1.observation.context is None
    assert v1.observation.feature_contract_version == MARKET_FEATURE_CONTRACT_VERSION
    assert "graph_features" not in v1.observation.to_dict()
    assert v3.observation.feature_contract_version == GRAPH_FEATURE_CONTRACT_VERSION
    assert v3.episode_id != v1.episode_id
    assert v3.observation.venue == v1.observation.venue
    assert v3.observation.symbol == v1.observation.symbol
    assert v3.observation.bar_interval == v1.observation.bar_interval
    assert v3.observation.as_of_bar_ts == v1.observation.as_of_bar_ts
    assert v3.observation.context is None
    snapshot = _graph_snapshot(v3.observation)
    assert snapshot is not None
    assert v3.observation.to_dict()["graph_features"]["snapshot"]["snapshot_id"] == snapshot.snapshot_id
    from trader.domain.world_episode import world_episode_id

    assert v3.episode_id == world_episode_id(
        venue=v3.observation.venue,
        symbol=v3.observation.symbol,
        bar_interval=v3.observation.bar_interval,
        as_of_bar_ts=v3.observation.as_of_bar_ts,
        feature_contract_version=GRAPH_FEATURE_CONTRACT_VERSION,
        sampling_policy_version=v3.observation.sampling_policy_version,
        graph_snapshot_id=snapshot.snapshot_id,
    )


def test_missing_unpublished_unmapped_and_budget_graph_are_neutral_v3_companions() -> None:
    v1 = _v1_episode()
    unpublished = _attach((v1,), _unpublished_config())[0]
    unmapped = _attach((v1,), _unmapped_config())[0]
    assert unpublished.training_eligible is True
    assert unmapped.training_eligible is True
    unpublished_snapshot = _graph_snapshot(unpublished.observation)
    unmapped_snapshot = _graph_snapshot(unmapped.observation)
    assert unpublished_snapshot.status == "missing"
    assert unpublished_snapshot.missingness["ontology"] == "unpublished"
    assert unmapped_snapshot.status == "missing"
    assert unmapped_snapshot.missingness["scope"] == "unmapped"
    assert unmapped_snapshot.structural_relation_refs == frozenset()
    assert unmapped_snapshot.knowledge_relation_refs == frozenset()
    features = _graph_features(unpublished.observation)["categorical_features"]
    assert features["graph_status"] == "missing"
    assert features["graph_missingness_status"] == "unpublished"
    assert v1.episode_id == V1_EPISODE_ID

    seeded = _complete_config(max_paths=1)
    complete_v1 = _graph6_v1_episode()
    budget = _attach((complete_v1,), seeded)[0]
    budget_snapshot = _graph_snapshot(budget.observation)
    assert budget_snapshot.status in {"partial", "complete"}
    if budget_snapshot.status == "partial":
        assert budget_snapshot.missingness["budget"] == "graph_budget_exceeded"
    budget_features = _graph_features(budget.observation)["categorical_features"]
    assert "graph_status" in budget_features
    assert complete_v1.observation.feature_contract_version == MARKET_FEATURE_CONTRACT_VERSION


def test_graph_cutoff_is_completed_bar_clock_and_unknown_semantics_fail_closed() -> None:
    from trader.application.world_model.capture import FEATURE_CONTRACT_VERSION
    from trader.domain.world_episode import AnchorBar, WorldObservation

    v1 = _graph6_v1_episode()
    v3 = _attach((v1,), _complete_config())[0]
    snapshot = _graph_snapshot(v3.observation)
    assert snapshot.cutoff_at == datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc)
    assert snapshot.cutoff_at == v1.observation.as_of_bar_ts
    assert v3.observation.available_at == v1.observation.available_at

    unknown = WorldEpisode(
        WorldObservation(
            venue="TW",
            symbol="2330",
            bar_interval="1h",
            as_of_bar_ts="2026-08-23T13:00:00+00:00",
            feature_contract_version=FEATURE_CONTRACT_VERSION,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts="2026-08-23T13:00:00+00:00",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
                source="unit-graph-7",
                timestamp_semantics="unknown",
            ),
            available_at="2026-08-23T13:00:00+00:00",
            captured_at="2026-08-23T13:00:00+00:00",
            freshness={"status": "fresh", "data_age_minutes": 5.0},
            categorical_features={"venue": "TW"},
            numeric_features={"return": 0.01},
        )
    )
    assert _attach((unknown,), _complete_config()) == ()


def test_encoded_graph_features_merge_into_v3_and_masks_are_not_contracts() -> None:
    v1 = _graph6_v1_episode()
    content = _attach((v1,), _complete_config(feature_mask=world_v3_graph_content_mask()))[0]
    status = _attach((v1,), _complete_config(feature_mask=world_v3_topology_status_only_mask()))[0]
    assert content.observation.feature_contract_version == status.observation.feature_contract_version
    assert content.observation.feature_contract_version == GRAPH_FEATURE_CONTRACT_VERSION
    content_cats = _graph_features(content.observation)["categorical_features"]
    status_cats = _graph_features(status.observation)["categorical_features"]
    assert GRAPH_STATUS_CATEGORICAL_FEATURES <= set(content_cats)
    assert GRAPH_CONTENT_CATEGORICAL_FEATURES <= set(content_cats)
    assert GRAPH_STATUS_CATEGORICAL_FEATURES <= set(status_cats)
    assert set(status_cats).isdisjoint(GRAPH_CONTENT_CATEGORICAL_FEATURES)
    assert "graph_path_signature" not in status_cats
    assert content_cats["graph_status"] == "complete"
    assert content_cats["graph_scope_status"] == "resolved"


def test_v2_is_not_widened_and_v3_does_not_replace_v1() -> None:
    v1 = _v1_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    v3_from_v2 = _attach((v2,), _unmapped_config())
    v3 = _attach((v1,), _unmapped_config())[0]
    assert v3_from_v2 == ()
    assert v2.observation.context is not None
    assert "graph_features" not in v2.observation.to_dict()
    assert v1.observation.context is None
    assert v3.observation.feature_contract_version == GRAPH_FEATURE_CONTRACT_VERSION


def test_snapshot_ledger_is_canonical_first_write_and_conflicts_on_payload_drift() -> None:
    from trader.application.world_model.graph_capture import WorldGraphCaptureConfig

    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    snapshots = _InMemorySnapshotLedger()
    config = WorldGraphCaptureConfig(
        scope_mapping=mapping,
        snapshot_service=WorldGraphSnapshotService(
            ledger=ledger,
            traversal=_service(ledger)._traversal,
            snapshot_ledger=snapshots,
        ),
        study_cohort_id="world_cohort:v1:" + "a" * 64,
    )
    v1 = _graph6_v1_episode()
    first = _attach((v1,), config)[0]
    assert len(snapshots.rows) == 1
    second = _attach((v1,), config)[0]
    assert len(snapshots.rows) == 1
    assert first.episode_id == second.episode_id
    assert config.study_cohort_id is not None


def test_graph_builder_failure_is_fail_open_and_does_not_drop_v1() -> None:
    from trader.application.world_model.graph_capture import (
        WorldGraphCaptureConfig,
        capture_world_episodes_with_graph,
    )

    class BoomService:
        def build(self, request):
            raise RuntimeError("graph boom")

    v1 = _v1_episode()
    config = WorldGraphCaptureConfig(
        scope_mapping=_empty_mapping(),
        snapshot_service=BoomService(),
        study_cohort_id=None,
    )
    assert _attach((v1,), config) == ()
    assert v1.episode_id == V1_EPISODE_ID
    v1_again, v3 = capture_world_episodes_with_graph(
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
        config=config,
        source="unit-market-bars",
        interval="1h",
        timestamp_semantics="bar_close",
        captured_at="2026-08-22T10:30:00+00:00",
    )
    assert len(v1_again) == 1
    assert v1_again[0].episode_id == V1_EPISODE_ID
    assert v3 == ()


def test_injected_study_cohort_id_does_not_invent_a_scope_mapping() -> None:
    from trader.application.world_model.graph_capture import WorldGraphCaptureConfig

    mapping = _mapping()
    config = WorldGraphCaptureConfig(
        scope_mapping=mapping,
        snapshot_service=_service(_InMemoryWorldGraphLedger()),
        study_cohort_id="world_cohort:v1:" + "b" * 64,
    )
    assert config.study_cohort_id.endswith("b" * 64)
    assert config.scope_mapping is mapping
    resolution = mapping.resolve(WorldMarketAnchorRef(market_venue="TW", instrument="2330"))
    assert isinstance(resolution, WorldScopeResolution)
    v1 = _v1_episode()
    v3 = _attach((v1,), _unmapped_config(study_cohort_id=config.study_cohort_id))[0]
    payload = v3.to_dict()
    assert "study_cohort_id" not in payload
    assert "study_cohort_id" not in payload["observation"]
