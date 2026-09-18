from __future__ import annotations

import ast
import inspect
import sys
from dataclasses import FrozenInstanceError

import pytest
import yaml

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_context import (
    ALLOWED_CONTEXT_CATEGORICAL_FEATURES,
    ALLOWED_CONTEXT_NUMERIC_FEATURES,
    CONTEXT_FEATURE_CONTRACT_ID,
    ONTOLOGY_REVISION,
)
from trader.domain.world_episode import (
    ALLOWED_CATEGORICAL_FEATURES,
    ALLOWED_NUMERIC_FEATURES,
    MARKET_FEATURE_CONTRACT_ID,
    canonical_payload,
)
from trader.domain.world_feature_contract import (
    GRAPH_CONTENT_CATEGORICAL_FEATURES,
    GRAPH_FEATURE_CONTRACT_ID,
    GRAPH_FEATURE_GROUP_ID,
    GRAPH_STATUS_CATEGORICAL_FEATURES,
    GRAPH_STATUS_FEATURE_GROUP_ID,
    WORLD_FEATURE_CONTRACT_SCHEMA,
    WORLD_FEATURE_MASK_SCHEMA,
    WORLD_GRAPH_CONFIG_SHA256,
    MARKET_ONTOLOGY_REVISION,
    GRAPH_PATH_RULE_VERSION,
    GRAPH_WINDOWS_AND_DECAY,
    WORLD_SCOPE_MAPPING_ID,
    MARKET_ENCODER_IDENTITY,
    CONTEXT_ENCODER_IDENTITY,
    GRAPH_ENCODER_IDENTITY,
    GRAPH_CONTENT_MASK_ID,
    GRAPH_GRU_MODEL_IDENTITY,
    GRAPH_MARKOV_MODEL_IDENTITY,
    GRAPH_MODEL_VERSION,
    TOPOLOGY_STATUS_ONLY_MASK_ID,
    WorldFeatureContract,
    WorldFeatureGroup,
    WorldFeatureMask,
    market_feature_contract,
    context_feature_contract,
    graph_feature_contract,
    graph_content_mask,
    topology_status_only_mask,
)
from trader.domain.world_scope import WorldScopeMapping, WorldScopeResolution


MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_feature_contract.py"

STATUS_CATEGORICAL = frozenset(
    {
        "context_status",
        "macro_status",
        "company_status",
        "company_coverage_status",
        "company_freshness_status",
        "company_source_count_bucket",
    }
)
COMPANY_CATEGORICAL = frozenset({"company_thesis_status"})
MACRO_CATEGORICAL = frozenset(
    {
        "context_macro_regime",
        "context_rates_regime",
        "context_usd_regime",
    }
)


def _group(
    group_id: str,
    *,
    categorical: frozenset[str] | tuple[str, ...] = (),
    numeric: frozenset[str] | tuple[str, ...] = (),
) -> WorldFeatureGroup:
    return WorldFeatureGroup(
        group_id=group_id,
        categorical_features=categorical,
        numeric_features=numeric,
    )


def _contract(**overrides: object) -> WorldFeatureContract:
    values: dict[str, object] = {
        "contract_id": "custom.v9",
        "accepted_episode_contract": MARKET_FEATURE_CONTRACT_ID,
        "projection_version": "custom_projection.v9",
        "encoder_identity": "world_feature_encoder.custom.v9",
        "groups": (_group("market", categorical=frozenset({"venue"}), numeric=frozenset({"return"})),),
        "ontology_revision": "custom_ontology.v9",
        "vocabulary_version": "custom_vocab.v9",
    }
    values.update(overrides)
    return WorldFeatureContract(**values)  # type: ignore[arg-type]


def test_market_and_context_profiles_are_frozen_and_compatible_with_existing_episode_contracts() -> None:
    market = market_feature_contract()
    context = context_feature_contract()
    assert type(market) is WorldFeatureContract
    assert type(context) is WorldFeatureContract
    assert market.contract_id == MARKET_FEATURE_CONTRACT_ID
    assert market.accepted_episode_contract == MARKET_FEATURE_CONTRACT_ID
    assert market.projection_version == MARKET_FEATURE_CONTRACT_ID
    assert market.encoder_identity == MARKET_ENCODER_IDENTITY == "world_feature_encoder.market.v2"
    assert market.vocabulary_version == MARKET_FEATURE_CONTRACT_ID
    assert context.contract_id == CONTEXT_FEATURE_CONTRACT_ID
    assert context.accepted_episode_contract == CONTEXT_FEATURE_CONTRACT_ID
    assert context.projection_version == CONTEXT_FEATURE_CONTRACT_ID
    assert context.encoder_identity == CONTEXT_ENCODER_IDENTITY == "world_feature_encoder.context.v1"
    assert context.vocabulary_version == CONTEXT_FEATURE_CONTRACT_ID
    assert market.schema_version == WORLD_FEATURE_CONTRACT_SCHEMA
    assert context.ontology_revision == ONTOLOGY_REVISION
    assert market.allowed_feature_groups == frozenset({"market"})
    assert context.allowed_feature_groups == frozenset({"market", "status", "company", "macro"})
    assert market.group("market").categorical_features == ALLOWED_CATEGORICAL_FEATURES
    assert market.group("market").numeric_features == ALLOWED_NUMERIC_FEATURES
    assert context.group("market").categorical_features == ALLOWED_CATEGORICAL_FEATURES
    assert context.group("market").numeric_features == ALLOWED_NUMERIC_FEATURES
    assert context.group("status").categorical_features == STATUS_CATEGORICAL
    assert context.group("company").categorical_features == COMPANY_CATEGORICAL
    assert context.group("macro").categorical_features == MACRO_CATEGORICAL
    assert context.group("macro").numeric_features == ALLOWED_CONTEXT_NUMERIC_FEATURES
    assert STATUS_CATEGORICAL | COMPANY_CATEGORICAL | MACRO_CATEGORICAL == ALLOWED_CONTEXT_CATEGORICAL_FEATURES
    assert market.fingerprint != context.fingerprint
    assert market.vocabulary_fingerprint != context.vocabulary_fingerprint
    assert market.path_rule_version is None
    assert market.windows_and_decay is None
    assert context.path_rule_version is None
    replayed = WorldFeatureContract.from_mapping(market.to_dict())
    assert replayed == market
    with pytest.raises(FrozenInstanceError):
        market.contract_id = "mutated"  # type: ignore[misc]


def test_include_context_is_not_a_contract_field() -> None:
    assert "include_context" not in inspect.signature(WorldFeatureContract).parameters
    graph = _contract(
        contract_id="world_feature.graph.v1",
        accepted_episode_contract="world_feature.graph.v1",
        projection_version="world_feature.graph.v1",
        encoder_identity="world_feature_encoder.graph.v1",
        groups=(
            _group("market", categorical=frozenset({"venue"})),
            _group("graph", categorical=frozenset({"graph_status"})),
        ),
        ontology_revision=ONTOLOGY_REVISION,
        vocabulary_version="graph_vocab.v1",
        path_rule_version="acyclic_paths.v1",
        windows_and_decay={"max_depth": 4, "decay": "none.v1"},
    )
    assert type(graph) is WorldFeatureContract
    assert type(graph) is type(market_feature_contract())
    assert type(graph) is type(context_feature_contract())
    assert graph.path_rule_version == "acyclic_paths.v1"
    assert dict(graph.windows_and_decay) == {"decay": "none.v1", "max_depth": 4}
    assert graph.fingerprint != market_feature_contract().fingerprint
    assert graph.fingerprint != context_feature_contract().fingerprint
    assert not hasattr(graph, "include_context_compatibility_flag")


def test_mask_selects_only_declared_groups_and_has_a_distinct_fingerprint() -> None:
    v2 = context_feature_contract()
    status_only = WorldFeatureMask.bind(
        v2,
        mask_id="status_only.v1",
        selected_groups=("status", "market"),
    )
    company = WorldFeatureMask.bind(
        v2,
        mask_id="company.v1",
        selected_groups=("market", "status", "company"),
    )
    market = WorldFeatureMask.bind(
        market_feature_contract(),
        mask_id="market.v1",
        selected_groups=("market",),
    )
    assert status_only.schema_version == WORLD_FEATURE_MASK_SCHEMA
    assert status_only.contract_id == v2.contract_id
    assert status_only.contract_fingerprint == v2.fingerprint
    assert status_only.selected_groups == ("market", "status")
    assert status_only.fingerprint != v2.fingerprint
    assert status_only.fingerprint != company.fingerprint
    assert status_only.fingerprint != market.fingerprint
    assert status_only.selected_categorical_features(v2) == ALLOWED_CATEGORICAL_FEATURES | STATUS_CATEGORICAL
    assert "company_thesis_status" not in status_only.selected_categorical_features(v2)
    assert "context_macro_regime" not in status_only.selected_categorical_features(v2)
    assert COMPANY_CATEGORICAL <= company.selected_categorical_features(v2)
    replayed = WorldFeatureMask.from_mapping(status_only.to_dict())
    assert replayed == status_only
    with pytest.raises(ValueError, match="group"):
        WorldFeatureMask.bind(v2, mask_id="graph.v1", selected_groups=("graph",))
    with pytest.raises(ValueError, match="fingerprint"):
        WorldFeatureMask.from_mapping({**status_only.to_dict(), "fingerprint": "0" * 64})


def test_reusing_an_id_with_another_fingerprint_is_a_conflict() -> None:
    v1 = market_feature_contract()
    other = _contract(contract_id=v1.contract_id, encoder_identity="other.encoder.v1")
    assert other.fingerprint != v1.fingerprint
    with pytest.raises(ValueError, match="fingerprint"):
        v1.assert_identity_compatible(other)
    v1.assert_identity_compatible(market_feature_contract())
    mask = WorldFeatureMask.bind(v1, mask_id="market.v1", selected_groups=("market",))
    other_mask = WorldFeatureMask.bind(other, mask_id="market.v1", selected_groups=("market",))
    with pytest.raises(ValueError, match="fingerprint"):
        mask.assert_identity_compatible(other_mask)


def test_fingerprint_is_order_insensitive_and_rejects_mismatched_digest() -> None:
    left = _contract(
        groups=(
            _group("macro", categorical=("context_usd_regime", "context_macro_regime")),
            _group("status", categorical=("macro_status", "context_status")),
        )
    )
    right = _contract(
        groups=(
            _group("status", categorical=("context_status", "macro_status")),
            _group("macro", categorical=("context_macro_regime", "context_usd_regime")),
        )
    )
    assert left.fingerprint == right.fingerprint
    assert left.vocabulary_fingerprint == right.vocabulary_fingerprint
    assert left.to_dict()["groups"] == right.to_dict()["groups"]
    payload = left.to_dict()
    with pytest.raises(ValueError, match="fingerprint"):
        WorldFeatureContract.from_mapping({**payload, "fingerprint": "0" * 64})
    with pytest.raises(ValueError, match="vocabulary_fingerprint"):
        WorldFeatureContract.from_mapping({**payload, "vocabulary_fingerprint": "0" * 64})


def test_groups_reject_duplicates_overlap_empty_names_and_action_keys() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _contract(
            groups=(
                _group("market", categorical=frozenset({"venue"})),
                _group("market", categorical=frozenset({"asset_family"})),
            )
        )
    with pytest.raises(ValueError, match="both categorical and numeric"):
        _group("market", categorical=frozenset({"return"}), numeric=frozenset({"return"}))
    with pytest.raises(ValueError, match="overlap"):
        _contract(
            groups=(
                _group("market", categorical=frozenset({"venue"})),
                _group("status", categorical=frozenset({"venue"})),
            )
        )
    with pytest.raises(ValueError, match="group_id"):
        _group(" ", categorical=frozenset({"venue"}))
    with pytest.raises(ValueError, match="at least one"):
        _group("market")
    with pytest.raises(ValueError, match="forbidden"):
        _group("market", categorical=frozenset({"portfolio_pnl"}))
    with pytest.raises(ValueError, match="groups"):
        _contract(groups=())
    with pytest.raises(ValueError, match="contract_id"):
        _contract(contract_id=" ")
    with pytest.raises(ValueError, match="schema_version"):
        _contract(schema_version="world_feature_contract.v0")


def test_mask_is_deeply_immutable_and_requires_a_non_empty_declared_subset() -> None:
    contract = context_feature_contract()
    mask = WorldFeatureMask.bind(contract, mask_id="status_only.v1", selected_groups=("market", "status"))
    with pytest.raises(FrozenInstanceError):
        mask.mask_id = "mutated"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        mask.selected_groups.append("company")  # type: ignore[attr-defined]
    assert not hasattr(mask, "windows_and_decay")
    with pytest.raises(ValueError, match="selected_groups"):
        WorldFeatureMask.bind(contract, mask_id="empty.v1", selected_groups=())
    with pytest.raises(ValueError, match="duplicate"):
        WorldFeatureMask.bind(contract, mask_id="dup.v1", selected_groups=("market", "market"))
    with pytest.raises(ValueError, match="mask_id"):
        WorldFeatureMask.bind(contract, mask_id=" ", selected_groups=("market",))
    foreign = market_feature_contract()
    with pytest.raises(ValueError, match="contract"):
        mask.assert_compatible_with(foreign)


def test_windows_and_decay_are_frozen_and_hashed_on_market_context_even_when_null() -> None:
    v3 = _contract(
        contract_id="world_feature.graph.v1",
        path_rule_version="acyclic_paths.v1",
        windows_and_decay={"max_depth": 4, "decay": "none.v1"},
    )
    with pytest.raises(TypeError):
        v3.windows_and_decay["max_depth"] = 8  # type: ignore[index]
    replayed = WorldFeatureContract.from_mapping(v3.to_dict())
    assert replayed.windows_and_decay == v3.windows_and_decay
    v1 = market_feature_contract()
    v2 = context_feature_contract()
    for contract in (v1, v2):
        payload = contract.content_payload()
        assert payload["path_rule_version"] is None
        assert payload["windows_and_decay"] is None
        unsigned = {key: value for key, value in contract.to_dict().items() if key != "fingerprint"}
        assert WorldFeatureContract.from_mapping(unsigned).fingerprint == contract.fingerprint
        with_path = WorldFeatureContract.from_mapping({**unsigned, "path_rule_version": "acyclic_paths.v1"})
        with_windows = WorldFeatureContract.from_mapping({**unsigned, "windows_and_decay": {"max_depth": 4}})
        assert with_path.fingerprint != contract.fingerprint
        assert with_windows.fingerprint != contract.fingerprint
        assert with_path.fingerprint != with_windows.fingerprint


def test_nested_windows_and_decay_cannot_corrupt_fingerprint_after_construction() -> None:
    contract = _contract(windows_and_decay={"nested": {"items": ["a"]}})
    fingerprint = contract.fingerprint
    exported = contract.to_dict()
    assert exported["windows_and_decay"] == {"nested": {"items": ["a"]}}
    assert isinstance(exported["windows_and_decay"]["nested"]["items"], list)

    nested_items = contract.windows_and_decay["nested"]["items"]
    with pytest.raises((TypeError, AttributeError)):
        nested_items.append("b")  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        nested_items[0] = "z"  # type: ignore[index]
    with pytest.raises(TypeError):
        contract.windows_and_decay["nested"]["items"] = ["b"]  # type: ignore[index]
    with pytest.raises(TypeError):
        contract.windows_and_decay["nested"]["other"] = 1  # type: ignore[index]

    exported["windows_and_decay"]["nested"]["items"].append("b")
    assert contract.fingerprint == fingerprint
    assert contract.to_dict()["windows_and_decay"] == {"nested": {"items": ["a"]}}
    replayed = WorldFeatureContract.from_mapping(contract.to_dict())
    assert replayed == contract
    assert replayed.fingerprint == fingerprint
    assert replayed.to_dict() == contract.to_dict()
    assert replayed.to_dict()["windows_and_decay"] == {"nested": {"items": ["a"]}}


def test_scope_mapping_types_remain_macro0_owners_not_a_cohort_contract() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "class WorldScopeMapping" not in source
    assert "class WorldScopeResolution" not in source
    assert "MARKET_ONTOLOGY_SHA256" not in source
    assert "WORLD_SCOPE_MAPPING_SHA256" not in source
    assert WorldScopeMapping.__module__ == "trader.domain.world_scope"
    assert WorldScopeResolution.__module__ == "trader.domain.world_scope"
    assert WorldFeatureContract.__module__ == "trader.domain.world_feature_contract"
    assert WorldFeatureMask.__module__ == "trader.domain.world_feature_contract"
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "numpy" not in source.lower()
    assert "networkx" not in source.lower()
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


def _committed_graph_config() -> dict[str, object]:
    return yaml.safe_load((REPO_ROOT / "config" / "world_graph.yaml").read_text(encoding="utf-8"))


def _committed_scope_mapping() -> dict[str, object]:
    return yaml.safe_load((REPO_ROOT / "config" / "world_scope_mapping.yaml").read_text(encoding="utf-8"))


def test_graph_contract_is_isolated_from_market_context_and_binds_committed_graph_config() -> None:
    v1 = market_feature_contract()
    v2 = context_feature_contract()
    v3 = graph_feature_contract()
    config = _committed_graph_config()
    mapping = _committed_scope_mapping()

    assert GRAPH_FEATURE_CONTRACT_ID == "world_feature.graph.v1"
    assert v3.contract_id == GRAPH_FEATURE_CONTRACT_ID
    assert v3.accepted_episode_contract == GRAPH_FEATURE_CONTRACT_ID
    assert v3.projection_version == GRAPH_FEATURE_CONTRACT_ID
    assert v3.encoder_identity == GRAPH_ENCODER_IDENTITY == "world_feature_encoder.graph.v1"
    assert v3.vocabulary_version == GRAPH_FEATURE_CONTRACT_ID
    assert v3.ontology_revision == MARKET_ONTOLOGY_REVISION == config["ontology_revision"] == "market_ontology.v1"
    assert len(str(config["content_sha256"])) == 64
    assert v3.path_rule_version == GRAPH_PATH_RULE_VERSION == config["traversal_policy_version"] == "graph_traversal.v1"
    assert v3.to_dict()["windows_and_decay"] == config["windows_and_decay"]
    assert canonical_payload(GRAPH_WINDOWS_AND_DECAY) == config["windows_and_decay"]
    assert WORLD_GRAPH_CONFIG_SHA256 == config["content_sha256"]
    assert WORLD_SCOPE_MAPPING_ID == config["scope_mapping"]["mapping_id"] == mapping["mapping_id"]
    assert "mapping_sha256" not in config["scope_mapping"]
    from trader.domain.world_scope import WorldScopeMapping

    loaded = WorldScopeMapping.from_mapping(
        {
            "schema_version": mapping.get("schema_version"),
            "mapping_id": mapping.get("mapping_id"),
            "entries": mapping.get("entries") or (),
            "content_sha256": mapping.get("content_sha256"),
        }
    )
    assert loaded.content_sha256 == mapping["content_sha256"]
    assert v3.allowed_feature_groups == frozenset({"market", "status", "company", "macro", "graph_status", "graph"})
    assert (
        v3.group("market").categorical_features
        == v2.group("market").categorical_features
        == ALLOWED_CATEGORICAL_FEATURES
    )
    assert v3.group("market").numeric_features == v2.group("market").numeric_features
    assert v3.group("status").categorical_features == STATUS_CATEGORICAL
    assert v3.group("company").categorical_features == COMPANY_CATEGORICAL
    assert v3.group("macro").categorical_features == MACRO_CATEGORICAL
    assert v3.group("macro").numeric_features == ALLOWED_CONTEXT_NUMERIC_FEATURES
    assert v3.group(GRAPH_STATUS_FEATURE_GROUP_ID).categorical_features == GRAPH_STATUS_CATEGORICAL_FEATURES
    assert v3.group(GRAPH_FEATURE_GROUP_ID).categorical_features == GRAPH_CONTENT_CATEGORICAL_FEATURES
    assert GRAPH_STATUS_CATEGORICAL_FEATURES.isdisjoint(GRAPH_CONTENT_CATEGORICAL_FEATURES)
    assert GRAPH_STATUS_CATEGORICAL_FEATURES.isdisjoint(ALLOWED_CATEGORICAL_FEATURES)
    assert GRAPH_CONTENT_CATEGORICAL_FEATURES.isdisjoint(ALLOWED_CONTEXT_CATEGORICAL_FEATURES)
    assert "graph_status" in GRAPH_STATUS_CATEGORICAL_FEATURES
    assert "graph_missingness_status" in GRAPH_STATUS_CATEGORICAL_FEATURES
    assert "graph_path_signature" in GRAPH_CONTENT_CATEGORICAL_FEATURES
    assert v3.fingerprint != v1.fingerprint
    assert v3.fingerprint != v2.fingerprint
    assert v3.vocabulary_fingerprint != v1.vocabulary_fingerprint
    assert v3.vocabulary_fingerprint != v2.vocabulary_fingerprint
    replayed = WorldFeatureContract.from_mapping(v3.to_dict())
    assert replayed == v3
    assert not hasattr(v3, "include_context_compatibility_flag")
    with pytest.raises(FrozenInstanceError):
        v3.contract_id = "mutated"  # type: ignore[misc]


def test_graph_model_and_encoder_identities_are_distinct_matched_lane_ids() -> None:
    assert GRAPH_MARKOV_MODEL_IDENTITY == "hierarchical_dirichlet_world_baseline@graph.v1"
    assert GRAPH_GRU_MODEL_IDENTITY == "online_gru_world_challenger@graph.v1"
    assert GRAPH_MODEL_VERSION == "graph.v1"
    assert GRAPH_ENCODER_IDENTITY != MARKET_ENCODER_IDENTITY
    assert GRAPH_ENCODER_IDENTITY != CONTEXT_ENCODER_IDENTITY
    assert GRAPH_MARKOV_MODEL_IDENTITY != GRAPH_GRU_MODEL_IDENTITY
    assert "@graph.v1" in GRAPH_MARKOV_MODEL_IDENTITY
    assert "@graph.v1" in GRAPH_GRU_MODEL_IDENTITY


def test_topology_status_only_mask_excludes_graph_content_and_is_not_a_c1_profile() -> None:
    v3 = graph_feature_contract()
    status_only = topology_status_only_mask()
    content = graph_content_mask()
    assert status_only.mask_id == TOPOLOGY_STATUS_ONLY_MASK_ID == "topology_status_only.v1"
    assert content.mask_id == GRAPH_CONTENT_MASK_ID == "graph_content.v1"
    assert status_only.contract_id == v3.contract_id == GRAPH_FEATURE_CONTRACT_ID
    assert status_only.contract_fingerprint == v3.fingerprint
    assert set(status_only.selected_groups) == {"market", "status", "company", "macro", "graph_status"}
    assert set(content.selected_groups) == {"market", "status", "company", "macro", "graph_status", "graph"}
    selected = status_only.selected_categorical_features(v3)
    assert GRAPH_STATUS_CATEGORICAL_FEATURES <= selected
    assert selected.isdisjoint(GRAPH_CONTENT_CATEGORICAL_FEATURES)
    assert "graph_path_signature" not in selected
    assert "graph_path_count_bucket" not in selected
    content_selected = content.selected_categorical_features(v3)
    assert GRAPH_CONTENT_CATEGORICAL_FEATURES <= content_selected
    assert GRAPH_STATUS_CATEGORICAL_FEATURES <= content_selected
    assert status_only.fingerprint != content.fingerprint
    assert status_only.fingerprint != v3.fingerprint
    replayed = WorldFeatureMask.from_mapping(status_only.to_dict())
    assert replayed == status_only
    v1 = market_feature_contract()
    v2 = context_feature_contract()
    with pytest.raises(ValueError, match="contract"):
        status_only.assert_compatible_with(v1)
    with pytest.raises(ValueError, match="contract"):
        status_only.assert_compatible_with(v2)
    config = _committed_graph_config()
    assert "topology_status_only" in config["feature_profiles_planned"]
    assert "graph_content" in config["feature_profiles_planned"]


def test_market_and_context_contracts_do_not_declare_graph_groups() -> None:
    v1 = market_feature_contract()
    v2 = context_feature_contract()
    assert GRAPH_STATUS_FEATURE_GROUP_ID not in v1.allowed_feature_groups
    assert GRAPH_FEATURE_GROUP_ID not in v1.allowed_feature_groups
    assert GRAPH_STATUS_FEATURE_GROUP_ID not in v2.allowed_feature_groups
    assert GRAPH_FEATURE_GROUP_ID not in v2.allowed_feature_groups
    assert v1.contract_id == MARKET_FEATURE_CONTRACT_ID
    assert v2.contract_id == CONTEXT_FEATURE_CONTRACT_ID
    assert v1.path_rule_version is None
    assert v2.windows_and_decay is None
