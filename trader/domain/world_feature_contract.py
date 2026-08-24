"""Shared V1/V2/V3 World feature contract and mask identities.

Stdlib-only. Application encoders implement this pair; cohort, macro, and graph
do not define a concurrent contract type. ``include_context`` is a V1/V2
compatibility facade, not a field and not a V3 switch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from trader.domain.world_context import (
    ALLOWED_CONTEXT_CATEGORICAL_FEATURES,
    ALLOWED_CONTEXT_NUMERIC_FEATURES,
    CONTEXT_FEATURE_CONTRACT_VERSION,
    ONTOLOGY_REVISION,
)
from trader.domain.world_episode import (
    ALLOWED_CATEGORICAL_FEATURES,
    ALLOWED_NUMERIC_FEATURES,
    MARKET_FEATURE_CONTRACT_VERSION,
    canonical_payload,
    canonical_sha256,
    validate_action_free_features,
)


WORLD_FEATURE_CONTRACT_SCHEMA = "world_feature_contract.v1"
WORLD_FEATURE_MASK_SCHEMA = "world_feature_mask.v1"
WORLD_V1_ENCODER_IDENTITY = "world_feature_encoder.v1"
WORLD_V2_ENCODER_IDENTITY = "world_feature_encoder.v2"
WORLD_V3_ENCODER_IDENTITY = "world_feature_encoder.v3"
GRAPH_FEATURE_CONTRACT_VERSION = "market_ohlcv_graph.v3"
MARKET_FEATURE_GROUP_ID = "market"
STATUS_FEATURE_GROUP_ID = "status"
COMPANY_FEATURE_GROUP_ID = "company"
MACRO_FEATURE_GROUP_ID = "macro"
GRAPH_STATUS_FEATURE_GROUP_ID = "graph_status"
GRAPH_FEATURE_GROUP_ID = "graph"
WORLD_V3_TOPOLOGY_STATUS_ONLY_MASK_ID = "topology_status_only.v1"
WORLD_V3_GRAPH_CONTENT_MASK_ID = "graph_content.v1"
WORLD_V3_MARKOV_MODEL_IDENTITY = "hierarchical_dirichlet_world_baseline@graph.v3"
WORLD_V3_GRU_MODEL_IDENTITY = "online_gru_world_challenger@graph.v3"
WORLD_V3_MODEL_VERSION = "graph.v3"
WORLD_GRAPH_V3_ONTOLOGY_REVISION = "market_ontology.v2"
WORLD_GRAPH_V3_ONTOLOGY_SHA256 = "fbeb05628eb2f8e0b6fc7723f4b731c99b3e0be6532aad524a2f98c6b4fb5542"
WORLD_GRAPH_V3_PATH_RULE_VERSION = "graph_traversal.v1"
WORLD_GRAPH_V3_CONFIG_SHA256 = "e8d1df0df1badfdd9c2004ae4cf9eacb9605347abacbd63563a844dcfe16ab84"
WORLD_SCOPE_MAPPING_ID = "world_scope_mapping.v2"
WORLD_SCOPE_MAPPING_SHA256 = "e1289a4a86e5cb07600cb3ac45122b170f1a0bb249db2fbb6e5c093a4a8a501b"
WORLD_GRAPH_V3_WINDOWS_AND_DECAY: Mapping[str, object] = MappingProxyType(
    {
        "windows": ("0-4h", "4-24h", "1-7d", "older"),
        "decay": "none",
        "aggregates": "counts_on_snapshot_members_only",
    }
)

_STATUS_CATEGORICAL_FEATURES = frozenset(
    {
        "context_status",
        "macro_status",
        "company_status",
        "company_coverage_status",
        "company_freshness_status",
        "company_source_count_bucket",
    }
)
_COMPANY_CATEGORICAL_FEATURES = frozenset({"company_thesis_status"})
_MACRO_CATEGORICAL_FEATURES = frozenset(
    {
        "context_macro_regime",
        "context_rates_regime",
        "context_usd_regime",
    }
)
GRAPH_STATUS_CATEGORICAL_FEATURES = frozenset(
    {
        "graph_status",
        "graph_scope_status",
        "graph_coverage_status",
        "graph_freshness_status",
        "graph_source_count_bucket",
        "graph_artifact_count_bucket",
        "graph_missingness_status",
    }
)
GRAPH_CONTENT_CATEGORICAL_FEATURES = frozenset(
    {
        "graph_path_count_bucket",
        "graph_depth_min_bucket",
        "graph_depth_max_bucket",
        "graph_path_freshness_min_bucket",
        "graph_path_freshness_max_bucket",
        "graph_path_signature",
        "graph_macro_agreement_status",
        "graph_window_0_4h_count_bucket",
        "graph_window_4_24h_count_bucket",
        "graph_window_1_7d_count_bucket",
        "graph_window_older_count_bucket",
    }
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _immutable_feature_names(value: Sequence[str] | frozenset[str] | None, field_name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (Sequence, frozenset, set)):
        raise TypeError(f"{field_name} must be a sequence of strings")
    names = [_required_text(item, f"{field_name}[]") for item in value]
    if len(names) != len(set(names)):
        raise ValueError(f"{field_name} must not contain duplicates")
    validate_action_free_features({name: "token" for name in names}, field_name)
    return frozenset(names)


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _windows_and_decay_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = canonical_payload(value)
    if not isinstance(payload, dict):
        raise TypeError("windows_and_decay must be a mapping")
    return payload


def _freeze_windows_and_decay(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("windows_and_decay must be a mapping")
    frozen = _deep_freeze(_windows_and_decay_payload(value))
    if not isinstance(frozen, Mapping):
        raise TypeError("windows_and_decay must be a mapping")
    return frozen


@dataclass(frozen=True)
class WorldFeatureGroup:
    """Named categorical/numeric allowlist declared by a WorldFeatureContract."""

    group_id: str
    categorical_features: Sequence[str] | frozenset[str] | None = None
    numeric_features: Sequence[str] | frozenset[str] | None = None

    def __post_init__(self) -> None:
        group_id = _required_text(self.group_id, "group_id")
        categorical = _immutable_feature_names(self.categorical_features, "categorical_features")
        numeric = _immutable_feature_names(self.numeric_features, "numeric_features")
        if not categorical and not numeric:
            raise ValueError("feature group must declare at least one feature")
        overlap = categorical & numeric
        if overlap:
            raise ValueError("feature names cannot be both categorical and numeric in the same group")
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "categorical_features", categorical)
        object.__setattr__(self, "numeric_features", numeric)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "categorical_features": sorted(self.categorical_features),
            "numeric_features": sorted(self.numeric_features),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldFeatureGroup) -> WorldFeatureGroup:
        if isinstance(value, WorldFeatureGroup):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("feature group must be WorldFeatureGroup or a mapping")
        return cls(
            group_id=value.get("group_id"),
            categorical_features=value.get("categorical_features") or (),
            numeric_features=value.get("numeric_features") or (),
        )


def _group_tuple(value: Sequence[Any] | None) -> tuple[WorldFeatureGroup, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("groups must be a sequence of WorldFeatureGroup")
    return tuple(
        item if isinstance(item, WorldFeatureGroup) else WorldFeatureGroup.from_mapping(item) for item in value
    )


def _canonical_groups(groups: Sequence[WorldFeatureGroup]) -> tuple[WorldFeatureGroup, ...]:
    ordered = tuple(sorted(groups, key=lambda group: group.group_id))
    seen: dict[str, WorldFeatureGroup] = {}
    features_to_group: dict[str, str] = {}
    for group in ordered:
        if group.group_id in seen:
            raise ValueError("duplicate WorldFeatureContract group_id")
        seen[group.group_id] = group
        for feature_name in group.categorical_features | group.numeric_features:
            owner = features_to_group.get(feature_name)
            if owner is not None:
                raise ValueError(f"feature {feature_name!r} overlap between groups {owner!r} and {group.group_id!r}")
            features_to_group[feature_name] = group.group_id
    return ordered


def _vocabulary_payload(groups: Sequence[WorldFeatureGroup]) -> dict[str, Any]:
    return {"groups": [group.to_dict() for group in groups]}


@dataclass(frozen=True)
class WorldFeatureContract:
    """Canonical V1/V2/V3 feature contract: identities, allowlists, and fingerprints."""

    contract_id: str
    accepted_episode_contract: str
    projection_version: str
    encoder_identity: str
    groups: Sequence[WorldFeatureGroup | Mapping[str, Any]]
    ontology_revision: str
    vocabulary_version: str
    path_rule_version: str | None = None
    windows_and_decay: Mapping[str, Any] | None = None
    schema_version: str = WORLD_FEATURE_CONTRACT_SCHEMA
    vocabulary_fingerprint: str | None = None
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        contract_id = _required_text(self.contract_id, "contract_id")
        accepted_episode_contract = _required_text(self.accepted_episode_contract, "accepted_episode_contract")
        projection_version = _required_text(self.projection_version, "projection_version")
        encoder_identity = _required_text(self.encoder_identity, "encoder_identity")
        ontology_revision = _required_text(self.ontology_revision, "ontology_revision")
        vocabulary_version = _required_text(self.vocabulary_version, "vocabulary_version")
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_FEATURE_CONTRACT_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_FEATURE_CONTRACT_SCHEMA}")
        groups = _canonical_groups(_group_tuple(self.groups))
        if not groups:
            raise ValueError("groups must not be empty")
        path_rule_version = _optional_text(self.path_rule_version, "path_rule_version")
        windows_and_decay = _freeze_windows_and_decay(self.windows_and_decay)
        vocabulary_fingerprint = canonical_sha256(_vocabulary_payload(groups))
        if (
            self.vocabulary_fingerprint is not None
            and _required_text(self.vocabulary_fingerprint, "vocabulary_fingerprint") != vocabulary_fingerprint
        ):
            raise ValueError("vocabulary_fingerprint does not match the canonical WorldFeatureContract")
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(self, "accepted_episode_contract", accepted_episode_contract)
        object.__setattr__(self, "projection_version", projection_version)
        object.__setattr__(self, "encoder_identity", encoder_identity)
        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "ontology_revision", ontology_revision)
        object.__setattr__(self, "vocabulary_version", vocabulary_version)
        object.__setattr__(self, "path_rule_version", path_rule_version)
        object.__setattr__(self, "windows_and_decay", windows_and_decay)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "vocabulary_fingerprint", vocabulary_fingerprint)
        digest = canonical_sha256(self.content_payload())
        if self.fingerprint is not None and _required_text(self.fingerprint, "fingerprint") != digest:
            raise ValueError("fingerprint does not match the canonical WorldFeatureContract")
        object.__setattr__(self, "fingerprint", digest)

    @property
    def allowed_feature_groups(self) -> frozenset[str]:
        return frozenset(group.group_id for group in self.groups)

    def group(self, group_id: str) -> WorldFeatureGroup:
        wanted = _required_text(group_id, "group_id")
        for group in self.groups:
            if group.group_id == wanted:
                return group
        raise ValueError(f"unknown feature group: {wanted}")

    def include_context_compatibility_flag(self) -> bool:
        if self.contract_id == MARKET_FEATURE_CONTRACT_VERSION:
            return False
        if self.contract_id == CONTEXT_FEATURE_CONTRACT_VERSION:
            return True
        raise ValueError("include_context is only a V1/V2 compatibility facade")

    def assert_identity_compatible(self, other: WorldFeatureContract) -> None:
        if not isinstance(other, WorldFeatureContract):
            raise TypeError("identity comparison requires a WorldFeatureContract")
        if self.contract_id == other.contract_id and self.fingerprint != other.fingerprint:
            raise ValueError("contract_id reused with a different fingerprint")

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "accepted_episode_contract": self.accepted_episode_contract,
            "projection_version": self.projection_version,
            "encoder_identity": self.encoder_identity,
            "groups": [group.to_dict() for group in self.groups],
            "ontology_revision": self.ontology_revision,
            "vocabulary_version": self.vocabulary_version,
            "vocabulary_fingerprint": self.vocabulary_fingerprint,
            "path_rule_version": self.path_rule_version,
            "windows_and_decay": None
            if self.windows_and_decay is None
            else _windows_and_decay_payload(self.windows_and_decay),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_payload(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldFeatureContract) -> WorldFeatureContract:
        if isinstance(value, WorldFeatureContract):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("feature contract must be WorldFeatureContract or a mapping")
        return cls(
            contract_id=value.get("contract_id"),
            accepted_episode_contract=value.get("accepted_episode_contract"),
            projection_version=value.get("projection_version"),
            encoder_identity=value.get("encoder_identity"),
            groups=value.get("groups") or (),
            ontology_revision=value.get("ontology_revision"),
            vocabulary_version=value.get("vocabulary_version"),
            path_rule_version=value.get("path_rule_version"),
            windows_and_decay=value.get("windows_and_decay"),
            schema_version=value.get("schema_version", WORLD_FEATURE_CONTRACT_SCHEMA),
            vocabulary_fingerprint=value.get("vocabulary_fingerprint"),
            fingerprint=value.get("fingerprint"),
        )


def _selected_group_tuple(value: Sequence[str] | frozenset[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (Sequence, frozenset, set)):
        raise TypeError("selected_groups must be a sequence of strings")
    names = [_required_text(item, "selected_groups[]") for item in value]
    if len(names) != len(set(names)):
        raise ValueError("selected_groups must not contain duplicates")
    return tuple(sorted(names))


@dataclass(frozen=True)
class WorldFeatureMask:
    """Subset of a WorldFeatureContract's groups, with its own identity and fingerprint."""

    mask_id: str
    contract_id: str
    contract_fingerprint: str
    selected_groups: Sequence[str]
    schema_version: str = WORLD_FEATURE_MASK_SCHEMA
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        mask_id = _required_text(self.mask_id, "mask_id")
        contract_id = _required_text(self.contract_id, "contract_id")
        contract_fingerprint = _required_text(self.contract_fingerprint, "contract_fingerprint")
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_FEATURE_MASK_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_FEATURE_MASK_SCHEMA}")
        selected_groups = _selected_group_tuple(self.selected_groups)
        if not selected_groups:
            raise ValueError("selected_groups must not be empty")
        content_payload = {
            "schema_version": schema_version,
            "mask_id": mask_id,
            "contract_id": contract_id,
            "contract_fingerprint": contract_fingerprint,
            "selected_groups": list(selected_groups),
        }
        digest = canonical_sha256(content_payload)
        if self.fingerprint is not None and _required_text(self.fingerprint, "fingerprint") != digest:
            raise ValueError("fingerprint does not match the canonical WorldFeatureMask")
        object.__setattr__(self, "mask_id", mask_id)
        object.__setattr__(self, "contract_id", contract_id)
        object.__setattr__(self, "contract_fingerprint", contract_fingerprint)
        object.__setattr__(self, "selected_groups", selected_groups)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "fingerprint", digest)

    @classmethod
    def bind(
        cls,
        contract: WorldFeatureContract,
        *,
        mask_id: str,
        selected_groups: Sequence[str],
    ) -> WorldFeatureMask:
        if not isinstance(contract, WorldFeatureContract):
            raise TypeError("mask bind requires a WorldFeatureContract")
        mask = cls(
            mask_id=mask_id,
            contract_id=contract.contract_id,
            contract_fingerprint=contract.fingerprint,
            selected_groups=selected_groups,
        )
        mask.assert_compatible_with(contract)
        return mask

    def assert_compatible_with(self, contract: WorldFeatureContract) -> None:
        if not isinstance(contract, WorldFeatureContract):
            raise TypeError("mask compatibility requires a WorldFeatureContract")
        if self.contract_id != contract.contract_id:
            raise ValueError("mask contract_id does not match WorldFeatureContract")
        if self.contract_fingerprint != contract.fingerprint:
            raise ValueError("mask contract_fingerprint does not match WorldFeatureContract")
        unknown = [group_id for group_id in self.selected_groups if group_id not in contract.allowed_feature_groups]
        if unknown:
            raise ValueError("mask selects groups not declared by WorldFeatureContract")

    def assert_identity_compatible(self, other: WorldFeatureMask) -> None:
        if not isinstance(other, WorldFeatureMask):
            raise TypeError("identity comparison requires a WorldFeatureMask")
        if self.mask_id == other.mask_id and self.fingerprint != other.fingerprint:
            raise ValueError("mask_id reused with a different fingerprint")

    def selected_categorical_features(self, contract: WorldFeatureContract) -> frozenset[str]:
        self.assert_compatible_with(contract)
        names: set[str] = set()
        for group_id in self.selected_groups:
            names.update(contract.group(group_id).categorical_features)
        return frozenset(names)

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mask_id": self.mask_id,
            "contract_id": self.contract_id,
            "contract_fingerprint": self.contract_fingerprint,
            "selected_groups": list(self.selected_groups),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_payload(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldFeatureMask) -> WorldFeatureMask:
        if isinstance(value, WorldFeatureMask):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("feature mask must be WorldFeatureMask or a mapping")
        return cls(
            mask_id=value.get("mask_id"),
            contract_id=value.get("contract_id"),
            contract_fingerprint=value.get("contract_fingerprint"),
            selected_groups=value.get("selected_groups") or (),
            schema_version=value.get("schema_version", WORLD_FEATURE_MASK_SCHEMA),
            fingerprint=value.get("fingerprint"),
        )


def world_v1_feature_contract() -> WorldFeatureContract:
    return WorldFeatureContract(
        contract_id=MARKET_FEATURE_CONTRACT_VERSION,
        accepted_episode_contract=MARKET_FEATURE_CONTRACT_VERSION,
        projection_version=MARKET_FEATURE_CONTRACT_VERSION,
        encoder_identity=WORLD_V1_ENCODER_IDENTITY,
        groups=(
            WorldFeatureGroup(
                group_id=MARKET_FEATURE_GROUP_ID,
                categorical_features=ALLOWED_CATEGORICAL_FEATURES,
                numeric_features=ALLOWED_NUMERIC_FEATURES,
            ),
        ),
        ontology_revision="market_only.v1",
        vocabulary_version=MARKET_FEATURE_CONTRACT_VERSION,
    )


def world_v2_feature_contract() -> WorldFeatureContract:
    if _STATUS_CATEGORICAL_FEATURES | _COMPANY_CATEGORICAL_FEATURES | _MACRO_CATEGORICAL_FEATURES != (
        ALLOWED_CONTEXT_CATEGORICAL_FEATURES
    ):
        raise ValueError("V2 feature groups must partition the context categorical allowlist")
    return WorldFeatureContract(
        contract_id=CONTEXT_FEATURE_CONTRACT_VERSION,
        accepted_episode_contract=CONTEXT_FEATURE_CONTRACT_VERSION,
        projection_version=CONTEXT_FEATURE_CONTRACT_VERSION,
        encoder_identity=WORLD_V2_ENCODER_IDENTITY,
        groups=(
            WorldFeatureGroup(
                group_id=MARKET_FEATURE_GROUP_ID,
                categorical_features=ALLOWED_CATEGORICAL_FEATURES,
                numeric_features=ALLOWED_NUMERIC_FEATURES,
            ),
            WorldFeatureGroup(
                group_id=STATUS_FEATURE_GROUP_ID,
                categorical_features=_STATUS_CATEGORICAL_FEATURES,
            ),
            WorldFeatureGroup(
                group_id=COMPANY_FEATURE_GROUP_ID,
                categorical_features=_COMPANY_CATEGORICAL_FEATURES,
            ),
            WorldFeatureGroup(
                group_id=MACRO_FEATURE_GROUP_ID,
                categorical_features=_MACRO_CATEGORICAL_FEATURES,
                numeric_features=ALLOWED_CONTEXT_NUMERIC_FEATURES,
            ),
        ),
        ontology_revision=ONTOLOGY_REVISION,
        vocabulary_version=CONTEXT_FEATURE_CONTRACT_VERSION,
    )


def world_v3_feature_contract() -> WorldFeatureContract:
    overlap = GRAPH_STATUS_CATEGORICAL_FEATURES & GRAPH_CONTENT_CATEGORICAL_FEATURES
    if overlap:
        raise ValueError("V3 graph status and content groups must be disjoint")
    v2 = world_v2_feature_contract()
    return WorldFeatureContract(
        contract_id=GRAPH_FEATURE_CONTRACT_VERSION,
        accepted_episode_contract=GRAPH_FEATURE_CONTRACT_VERSION,
        projection_version=GRAPH_FEATURE_CONTRACT_VERSION,
        encoder_identity=WORLD_V3_ENCODER_IDENTITY,
        groups=(
            *v2.groups,
            WorldFeatureGroup(
                group_id=GRAPH_STATUS_FEATURE_GROUP_ID,
                categorical_features=GRAPH_STATUS_CATEGORICAL_FEATURES,
            ),
            WorldFeatureGroup(
                group_id=GRAPH_FEATURE_GROUP_ID,
                categorical_features=GRAPH_CONTENT_CATEGORICAL_FEATURES,
            ),
        ),
        ontology_revision=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        vocabulary_version=GRAPH_FEATURE_CONTRACT_VERSION,
        path_rule_version=WORLD_GRAPH_V3_PATH_RULE_VERSION,
        windows_and_decay=WORLD_GRAPH_V3_WINDOWS_AND_DECAY,
    )


def world_v3_topology_status_only_mask() -> WorldFeatureMask:
    return WorldFeatureMask.bind(
        world_v3_feature_contract(),
        mask_id=WORLD_V3_TOPOLOGY_STATUS_ONLY_MASK_ID,
        selected_groups=(
            MARKET_FEATURE_GROUP_ID,
            STATUS_FEATURE_GROUP_ID,
            COMPANY_FEATURE_GROUP_ID,
            MACRO_FEATURE_GROUP_ID,
            GRAPH_STATUS_FEATURE_GROUP_ID,
        ),
    )


def world_v3_graph_content_mask() -> WorldFeatureMask:
    return WorldFeatureMask.bind(
        world_v3_feature_contract(),
        mask_id=WORLD_V3_GRAPH_CONTENT_MASK_ID,
        selected_groups=(
            MARKET_FEATURE_GROUP_ID,
            STATUS_FEATURE_GROUP_ID,
            COMPANY_FEATURE_GROUP_ID,
            MACRO_FEATURE_GROUP_ID,
            GRAPH_STATUS_FEATURE_GROUP_ID,
            GRAPH_FEATURE_GROUP_ID,
        ),
    )


def world_feature_contract_for_include_context(include_context: bool) -> WorldFeatureContract:
    if include_context:
        return world_v2_feature_contract()
    return world_v1_feature_contract()


__all__ = [
    "COMPANY_FEATURE_GROUP_ID",
    "GRAPH_CONTENT_CATEGORICAL_FEATURES",
    "GRAPH_FEATURE_CONTRACT_VERSION",
    "GRAPH_FEATURE_GROUP_ID",
    "GRAPH_STATUS_CATEGORICAL_FEATURES",
    "GRAPH_STATUS_FEATURE_GROUP_ID",
    "MACRO_FEATURE_GROUP_ID",
    "MARKET_FEATURE_GROUP_ID",
    "STATUS_FEATURE_GROUP_ID",
    "WORLD_FEATURE_CONTRACT_SCHEMA",
    "WORLD_FEATURE_MASK_SCHEMA",
    "WORLD_GRAPH_V3_CONFIG_SHA256",
    "WORLD_GRAPH_V3_ONTOLOGY_REVISION",
    "WORLD_GRAPH_V3_ONTOLOGY_SHA256",
    "WORLD_GRAPH_V3_PATH_RULE_VERSION",
    "WORLD_GRAPH_V3_WINDOWS_AND_DECAY",
    "WORLD_SCOPE_MAPPING_ID",
    "WORLD_SCOPE_MAPPING_SHA256",
    "WORLD_V1_ENCODER_IDENTITY",
    "WORLD_V2_ENCODER_IDENTITY",
    "WORLD_V3_ENCODER_IDENTITY",
    "WORLD_V3_GRAPH_CONTENT_MASK_ID",
    "WORLD_V3_GRU_MODEL_IDENTITY",
    "WORLD_V3_MARKOV_MODEL_IDENTITY",
    "WORLD_V3_MODEL_VERSION",
    "WORLD_V3_TOPOLOGY_STATUS_ONLY_MASK_ID",
    "WorldFeatureContract",
    "WorldFeatureGroup",
    "WorldFeatureMask",
    "world_feature_contract_for_include_context",
    "world_v1_feature_contract",
    "world_v2_feature_contract",
    "world_v3_feature_contract",
    "world_v3_graph_content_mask",
    "world_v3_topology_status_only_mask",
]
