from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from tests.application.test_world_cohort_service import _MemoryWorldCohortStore
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.cohort_service import WorldCohortService
from trader.domain.world_cohort import (
    CohortPhase,
    InvalidateWorldCohort,
    InvalidationReason,
    ModelFamily,
    WorldCohortId,
    WorldRuntimeIdentity,
)
from trader.domain.world_episode import canonical_sha256
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_feature_contract import (
    WORLD_SCOPE_MAPPING_ID,
)
from trader.domain.world_ontology_lifecycle import market_ontology_revision_id


UTC = timezone.utc
BOOT = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
LATER_BOOT = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)
EXPIRED = datetime(2026, 8, 17, tzinfo=UTC)
CONFIG_PATH = REPO_ROOT / "config" / "world_shadow_pilot.yaml"
MODULE_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "pilot_activation.py"
C1_LOGICAL = ("market", "status_only", "company", "macro", "joint")
GRAPH_LOGICAL = ("graph",)


def _service():
    store = _MemoryWorldCohortStore()
    return WorldCohortService(repository=store, query=store), store


def _live_mapping():
    return WorldScopeResolver.load(REPO_ROOT / "config").mapping


def _activate(**kwargs):
    from trader.application.world_model.pilot_activation import activate_world_shadow_pilot

    values = {
        "config_dir": REPO_ROOT / "config",
        "now": BOOT,
        "environ": {},
        "mapping": _live_mapping(),
    }
    values.update(kwargs)
    if "cohort_service" not in values:
        service, _store = _service()
        values["cohort_service"] = service
    if "ontology_revision" not in values:
        # Default stand-in pin: the committed id for the mapping under test
        # (production passes ensure_published().revision_id, extended).
        values["ontology_revision"] = market_ontology_revision_id(values["mapping"])
    return activate_world_shadow_pilot(**values)


class _FixedIdentity:
    def __init__(self, identity: WorldRuntimeIdentity) -> None:
        self._identity = identity

    def measure(self) -> WorldRuntimeIdentity:
        return self._identity


def _identity(git_commit: str = "b" * 40) -> WorldRuntimeIdentity:
    return WorldRuntimeIdentity(
        git_commit=git_commit,
        python_version="3.11.9",
        numpy_version="1.26.4",
        application_build_id="casys-trader.world.shadow_pilot.v1",
    )


def _write_hashed_pilot_config(directory: Path, payload: dict) -> Path:
    hashed = {str(key): value for key, value in payload.items() if key != "content_sha256"}
    out = dict(payload)
    out["content_sha256"] = canonical_sha256(hashed)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "world_shadow_pilot.yaml").write_text(
        yaml.safe_dump(out, sort_keys=False),
        encoding="utf-8",
    )
    return directory


def test_committed_pilot_config_is_versioned_hashed_shadow_only_and_operator_authorized() -> None:
    from trader.application.world_model.pilot_activation import (
        WORLD_SHADOW_PILOT_SCHEMA,
        load_world_shadow_pilot_config,
    )

    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == WORLD_SHADOW_PILOT_SCHEMA == "world_shadow_pilot.v1"
    assert payload["pilot_id"] == "world_shadow_pilot.v1"
    assert "supersedes_pilot_id" not in payload
    assert payload["lifecycle_generation"] == 3
    assert payload["authority"] == "shadow_only"
    assert payload["decision_effect"] == "none"
    assert payload["recommendation"] == "NO_GO"
    assert payload["causal_claim"] is False
    assert payload["pnl_claim"] is False
    assert payload["activation_policy"] == "operator_authorized_on_boot"
    assert payload["enabled"] is True
    assert payload["horizons"] == ["elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1"]
    assert payload["primary_horizon"] == "elapsed_1d.v1"
    assert "runtime_identity" not in payload
    assert "git_commit" not in payload["runtime_identity_intent"]
    hashed = dict(payload)
    claimed = hashed.pop("content_sha256")
    assert claimed == canonical_sha256(hashed)
    config = load_world_shadow_pilot_config(REPO_ROOT / "config")
    assert config is not None
    assert config.content_sha256 == claimed
    assert config.activation_policy == "operator_authorized_on_boot"
    assert config.runtime_identity_intent.application_build_id == "casys-trader.world.shadow_pilot.v1"
    assert config.workers["market"] is True
    assert config.workers["context"] is True
    assert config.workers["macro_source"] is True
    assert config.workers["graph"] is True
    assert config.window["planned_start"] == "boot_event_time"
    assert config.window["duration_days"] == 7
    assert config.window["collection_stop_kind"] == "fixed_end"
    assert "2026-08-17" not in CONFIG_PATH.read_text(encoding="utf-8")
    assert payload["scope_mapping"]["mapping_id"] == WORLD_SCOPE_MAPPING_ID
    assert "mapping_sha256" not in payload["scope_mapping"]
    from trader.domain.world_macro import (
        MACRO_LANE_IDENTITY,
        MACRO_PRODUCER_VERSION,
        WORLD_MACRO_COLLECTION_PLAN_ID,
        WORLD_MACRO_COLLECTION_PLAN_SHA256,
    )

    macro = payload["macro_producer"]
    assert macro["producer_version"] == MACRO_PRODUCER_VERSION == "world_macro_source.v1"
    assert macro["lane_identity"] == MACRO_LANE_IDENTITY == "world.context.macro"
    assert macro["collection_plan_id"] == WORLD_MACRO_COLLECTION_PLAN_ID
    assert macro["collection_plan_sha256"] == WORLD_MACRO_COLLECTION_PLAN_SHA256
    source = CONFIG_PATH.read_text(encoding="utf-8")
    assert "gdelt" not in source.lower()
    assert "news_macro_brief" not in source.lower()
    assert "git_commit" not in source


def test_activation_module_documents_rfc_exception_and_stays_application_owned() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "operator_authorized_on_boot" in source
    assert "no implicit activation" in source.lower()
    assert "human override" in source.lower()
    tree = ast.parse(source, filename=str(MODULE_PATH))
    forbidden = ("trader.runtime", "trader.infrastructure", "trader.reporting")
    leaks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in forbidden):
                leaks.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in forbidden):
                    leaks.append(alias.name)
    assert leaks == []
    assert "backfill" in source.lower()
    assert "def backfill" not in source


def test_boot_materializes_a_usable_seven_day_window_from_start_not_an_expired_date() -> None:
    service, store = _service()
    report = _activate(cohort_service=service, now=BOOT)
    # No ontology proof: the graph cohort is blocked, and the status says so
    # instead of masking it behind "started".
    assert report.status == "blocked"
    assert report.authority == "shadow_only"
    assert report.decision_effect == "none"
    assert report.recommendation == "NO_GO"
    assert report.causal_claim is False
    assert report.episodes_appended == 0
    assert report.backfill is False
    assert report.window["planned_start_not_before"] == BOOT
    assert report.window["collection_stop_at"] == BOOT + timedelta(days=7)
    assert report.window["collection_stop_at"] != EXPIRED
    assert len(report.cohorts) == 2
    by_key = {item["key"]: item for item in report.cohorts}
    c1 = store.load(WorldCohortId(by_key["technical_c1"]["cohort_id"]))
    graph = store.load(WorldCohortId(by_key["graph"]["cohort_id"]))
    assert c1.phase is CohortPhase.COLLECTING
    assert graph.phase is CohortPhase.REGISTERED
    assert c1.started_event is not None
    assert graph.started_event is None
    for cohort in (c1, graph):
        assert cohort.manifest.planned_start_not_before == BOOT
        assert cohort.manifest.collection_stop_rule.at == BOOT + timedelta(days=7)
        assert cohort.manifest.bar_interval == "15m"
        assert cohort.manifest.authority == "shadow_only"
        assert cohort.manifest.decision_effect == "none"
    envelope = store.envelope_for(c1.started_event)
    assert envelope.require_proven() is not None


def test_second_boot_is_idempotent_repairs_receipts_and_never_moves_the_window() -> None:
    service, store = _service()
    store.crash_before_receipt = 1
    first = _activate(cohort_service=service, now=BOOT)
    assert any(item.availability_status == "availability_unproven" for item in store.envelopes.values())
    second = _activate(cohort_service=service, now=LATER_BOOT)
    assert second.status == "blocked"
    assert {item["cohort_id"] for item in first.cohorts} == {item["cohort_id"] for item in second.cohorts}
    assert second.window["planned_start_not_before"] == BOOT
    assert second.window["collection_stop_at"] == BOOT + timedelta(days=7)
    assert second.episodes_appended == 0
    assert second.backfill is False
    by_key = {item["key"]: item for item in second.cohorts}
    c1 = store.load(WorldCohortId(by_key["technical_c1"]["cohort_id"]))
    graph = store.load(WorldCohortId(by_key["graph"]["cohort_id"]))
    assert c1.phase is CohortPhase.COLLECTING
    assert graph.phase is CohortPhase.REGISTERED
    assert c1.manifest.planned_start_not_before == BOOT
    started_events = [event for event in c1.events if event.event_type == "world_cohort_started"]
    assert len(started_events) == 1
    assert store.envelope_for(c1.started_event).availability_status == "eligible"


def test_technical_c1_excludes_graph_and_graph_cohort_is_its_own_graph_lane() -> None:
    service, store = _service()
    report = _activate(cohort_service=service)
    by_key = {item["key"]: item for item in report.cohorts}
    assert set(by_key) == {"technical_c1", "graph"}
    c1 = store.load(WorldCohortId(by_key["technical_c1"]["cohort_id"]))
    graph = store.load(WorldCohortId(by_key["graph"]["cohort_id"]))
    c1_ids = {lane.lane_id for lane in c1.manifest.lanes}
    graph_ids = {lane.lane_id for lane in graph.manifest.lanes}
    expected_c1 = {f"{family}.{logical}" for family in ("markov", "gru") for logical in C1_LOGICAL}
    assert c1_ids == expected_c1
    assert not any("graph" in lane_id.split(".") for lane_id in c1_ids)
    assert graph_ids == {"markov.graph", "gru.graph"}
    families = {lane.lane_id: lane.model_family for lane in (*c1.manifest.lanes, *graph.manifest.lanes)}
    assert families["markov.market"] is ModelFamily.MARKOV
    assert families["gru.joint"] is ModelFamily.GRU
    gru_lanes = [lane for lane in c1.manifest.lanes if lane.model_family is ModelFamily.GRU]
    assert gru_lanes and all(lane.sequence_length == 4 for lane in gru_lanes)
    mapping = _live_mapping()
    assert c1.manifest.scope_mapping is not None
    assert c1.manifest.scope_mapping.mapping_id == WORLD_SCOPE_MAPPING_ID
    assert graph.manifest.scope_mapping.mapping_sha256 == mapping.content_sha256
    graph_masks = {lane.lane_id: lane.feature_mask_id for lane in graph.manifest.lanes}
    assert graph_masks["markov.graph"] == "topology_status_only.v1"
    assert graph_masks["gru.graph"] == "graph_content.v1"
    assert c1.manifest.ontology_revision == "semantic_catalog.v1"
    assert graph.manifest.ontology_revision == market_ontology_revision_id(_live_mapping())
    assert graph.phase is CohortPhase.REGISTERED
    from trader.domain.world_macro import MACRO_PRODUCER_VERSION

    macro_sensors = [item for item in c1.manifest.sensor_requirements if item.sensor_id == "macro"]
    assert macro_sensors and all(item.source_contract_id == MACRO_PRODUCER_VERSION for item in macro_sensors)


def test_env_and_config_disable_skip_register_without_raising() -> None:
    service, store = _service()
    skipped = _activate(cohort_service=service, environ={"CASYS_WORLD_SHADOW_PILOT_ACTIVATION": "0"})
    assert skipped.status == "skipped"
    assert skipped.reason == "env_disabled"
    assert skipped.episodes_appended == 0
    assert store.manifests == {}
    missing = _activate(cohort_service=service, config_dir=Path("/tmp/missing-world-shadow-pilot"))
    assert missing.status == "skipped"
    assert missing.reason in {"config_missing", "config_invalid"}
    assert store.manifests == {}


def test_next_cohort_can_add_three_day_metric_while_one_day_stays_primary(tmp_path: Path) -> None:
    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert payload["lifecycle_generation"] == 3
    assert payload["horizons"] == ["elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1"]
    assert payload["primary_horizon"] == "elapsed_1d.v1"
    config_dir = _write_hashed_pilot_config(tmp_path / "next", payload)
    service, store = _service()

    report = _activate(
        cohort_service=service,
        config_dir=config_dir,
        runtime_identity=_FixedIdentity(_identity()),
    )

    assert report.status == "blocked"
    for item in report.cohorts:
        cohort = store.load(WorldCohortId(item["cohort_id"]))
        assert cohort.manifest.horizons == ("elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1")
        assert cohort.manifest.primary_horizon == "elapsed_1d.v1"


def test_invalid_or_disabled_config_is_fail_open(tmp_path) -> None:
    broken = tmp_path / "world_shadow_pilot.yaml"
    broken.write_text("schema_version: world_shadow_pilot.v1\ncontent_sha256: '00'\n", encoding="utf-8")
    service, store = _service()
    invalid = _activate(cohort_service=service, config_dir=tmp_path)
    assert invalid.status == "skipped"
    assert invalid.reason == "config_invalid"
    assert store.manifests == {}

    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    payload["enabled"] = False
    hashed = dict(payload)
    hashed.pop("content_sha256", None)
    payload["content_sha256"] = canonical_sha256(hashed)
    disabled_dir = tmp_path / "disabled"
    disabled_dir.mkdir()
    (disabled_dir / "world_shadow_pilot.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    disabled = _activate(cohort_service=service, config_dir=disabled_dir)
    assert disabled.status == "skipped"
    assert disabled.reason == "disabled"
    assert store.manifests == {}


def test_activation_does_not_write_episodes_or_admit_pre_start_slots() -> None:
    service, store = _service()
    report = _activate(cohort_service=service)
    assert report.episodes_appended == 0
    assert not hasattr(store, "episodes") or store.episodes == {}
    for item in report.cohorts:
        cohort = store.load(WorldCohortId(item["cohort_id"]))
        assert cohort.admitted_slots == ()
        assert all(event.event_type != "world_cohort_slot_admitted" for event in cohort.events)


def test_lifecycle_generation_bump_mints_new_ids_and_does_not_revive_terminal_cohorts(
    tmp_path,
) -> None:
    service, store = _service()
    identity = _identity()
    drifted = _identity("c" * 40)
    first = _activate(cohort_service=service, runtime_identity=_FixedIdentity(identity))
    second = _activate(
        cohort_service=service,
        runtime_identity=_FixedIdentity(identity),
        now=LATER_BOOT,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    assert first_ids == {item["key"]: item["cohort_id"] for item in second.cohorts}
    assert second.window["planned_start_not_before"] == BOOT
    assert second.window["collection_stop_at"] == BOOT + timedelta(days=7)

    for cohort_id in first_ids.values():
        service.invalidate(
            WorldCohortId(cohort_id),
            InvalidateWorldCohort(
                reason=InvalidationReason.ACCEPTED_DRIFT,
                scope="cohort",
                proofs=("runtime_commit_changed",),
                occurred_at=LATER_BOOT,
            ),
        )
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.INVALIDATED

    retained = _activate(
        cohort_service=service,
        runtime_identity=_FixedIdentity(drifted),
        now=LATER_BOOT,
    )
    retained_ids = {item["key"]: item["cohort_id"] for item in retained.cohorts}
    assert retained_ids == first_ids
    for cohort_id in retained_ids.values():
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.INVALIDATED

    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    payload["lifecycle_generation"] = int(payload["lifecycle_generation"]) + 1
    next_dir = _write_hashed_pilot_config(tmp_path / "next_generation", payload)
    next_report = _activate(
        cohort_service=service,
        config_dir=next_dir,
        runtime_identity=_FixedIdentity(drifted),
        now=LATER_BOOT,
    )
    next_ids = {item["key"]: item["cohort_id"] for item in next_report.cohorts}
    assert set(next_ids) == {"technical_c1", "graph"}
    assert set(next_ids.values()).isdisjoint(first_ids.values())
    assert next_report.window["planned_start_not_before"] == LATER_BOOT
    assert next_report.window["collection_stop_at"] == LATER_BOOT + timedelta(days=7)
    assert store.load(WorldCohortId(next_ids["technical_c1"])).phase is CohortPhase.COLLECTING
    assert store.load(WorldCohortId(next_ids["graph"])).phase is CohortPhase.REGISTERED
    for cohort_id in first_ids.values():
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.INVALIDATED
