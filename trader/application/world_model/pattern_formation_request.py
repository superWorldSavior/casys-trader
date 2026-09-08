"""Validated request for explicit graph-pattern formation discovery."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trader.domain.world_episode import (
    DEFAULT_WORLD_HORIZONS,
    SUPPORTED_WORLD_HORIZONS,
    parse_utc_timestamp,
)
from trader.domain.world_feature_contract import graph_content_mask, graph_feature_contract
from trader.domain.world_pattern import EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _sha256_hex(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field_name} must be a sha256 hex digest")
    return text


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a positive integer")
    if value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a nonnegative integer")
    if value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    return value


def _finite_positive(value: Any, field_name: str) -> float:
    number = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or number != number or number in (
        float("inf"),
        float("-inf"),
    ):
        raise ValueError(f"{field_name} must be a finite number greater than 0")
    if number <= 0.0:
        raise ValueError(f"{field_name} must be a finite number greater than 0")
    return number


def _unit_interval(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number in [0, 1]")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < 0.0 or number > 1.0:
        raise ValueError(f"{field_name} must be in [0, 1]")
    return number


_SUPPORTED_HORIZONS = frozenset(item.horizon_id for item in SUPPORTED_WORLD_HORIZONS)
_DEFAULT_HORIZONS = tuple(item.horizon_id for item in DEFAULT_WORLD_HORIZONS)


@dataclass(frozen=True)
class PatternFormationRequest:
    """As-of discovery request. Graph content and explicit model identity are frozen."""

    formation_cutoff: datetime | str
    evaluation_start_not_before: datetime | str
    horizons: Sequence[str] = _DEFAULT_HORIZONS
    min_support: int = 20
    min_association: float = 0.10
    max_candidates: int = 20
    smoothing_alpha: float = 1.0
    model_identity: str = EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY
    feature_contract_id: str | None = None
    feature_contract_fingerprint: str | None = None
    feature_mask_id: str | None = None
    feature_mask_fingerprint: str | None = None
    ontology_revision: str | None = None

    def __post_init__(self) -> None:
        formation_cutoff = parse_utc_timestamp(self.formation_cutoff, "formation_cutoff")
        evaluation_start_not_before = parse_utc_timestamp(
            self.evaluation_start_not_before, "evaluation_start_not_before"
        )
        if not (formation_cutoff < evaluation_start_not_before):
            raise ValueError("evaluation_start_not_before must be strictly later than formation_cutoff")
        if isinstance(self.horizons, (str, bytes, bytearray)) or not isinstance(self.horizons, Sequence):
            raise TypeError("horizons must be a sequence of horizon ids")
        horizons = tuple(_required_text(item, "horizons[]") for item in self.horizons)
        if not horizons:
            raise ValueError("horizons must not be empty")
        if len(horizons) != len(set(horizons)):
            raise ValueError("horizons must not contain duplicates")
        unknown = [item for item in horizons if item not in _SUPPORTED_HORIZONS]
        if unknown:
            raise ValueError(f"unsupported horizon_id: {unknown[0]}")
        contract = graph_feature_contract()
        mask = graph_content_mask()
        contract_id = contract.contract_id if self.feature_contract_id is None else _required_text(
            self.feature_contract_id, "feature_contract_id"
        )
        contract_fp = (
            contract.fingerprint
            if self.feature_contract_fingerprint is None
            else _sha256_hex(self.feature_contract_fingerprint, "feature_contract_fingerprint")
        )
        mask_id = mask.mask_id if self.feature_mask_id is None else _required_text(self.feature_mask_id, "feature_mask_id")
        mask_fp = (
            mask.fingerprint
            if self.feature_mask_fingerprint is None
            else _sha256_hex(self.feature_mask_fingerprint, "feature_mask_fingerprint")
        )
        if contract_id != contract.contract_id or contract_fp != contract.fingerprint:
            raise ValueError("feature contract must be the current graph_feature_contract identity")
        if mask_id != mask.mask_id or mask_fp != mask.fingerprint:
            raise ValueError("feature mask must be the current graph_content_mask identity")
        model_identity = _required_text(self.model_identity, "model_identity")
        if model_identity != EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY:
            raise ValueError(f"model_identity must be {EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY}")
        object.__setattr__(self, "formation_cutoff", formation_cutoff)
        object.__setattr__(self, "evaluation_start_not_before", evaluation_start_not_before)
        object.__setattr__(self, "horizons", horizons)
        object.__setattr__(self, "min_support", _positive_int(self.min_support, "min_support"))
        object.__setattr__(self, "min_association", _unit_interval(self.min_association, "min_association"))
        object.__setattr__(self, "max_candidates", _non_negative_int(self.max_candidates, "max_candidates"))
        object.__setattr__(self, "smoothing_alpha", _finite_positive(self.smoothing_alpha, "smoothing_alpha"))
        object.__setattr__(self, "model_identity", model_identity)
        ontology_revision = None if self.ontology_revision is None else _required_text(
            self.ontology_revision, "ontology_revision"
        )
        object.__setattr__(self, "feature_contract_id", contract_id)
        object.__setattr__(self, "feature_contract_fingerprint", contract_fp)
        object.__setattr__(self, "feature_mask_id", mask_id)
        object.__setattr__(self, "feature_mask_fingerprint", mask_fp)
        object.__setattr__(self, "ontology_revision", ontology_revision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "formation_cutoff": self.formation_cutoff.isoformat(),
            "evaluation_start_not_before": self.evaluation_start_not_before.isoformat(),
            "horizons": list(self.horizons),
            "min_support": self.min_support,
            "min_association": self.min_association,
            "max_candidates": self.max_candidates,
            "smoothing_alpha": self.smoothing_alpha,
            "model_identity": self.model_identity,
            "feature_contract_id": self.feature_contract_id,
            "feature_contract_fingerprint": self.feature_contract_fingerprint,
            "feature_mask_id": self.feature_mask_id,
            "feature_mask_fingerprint": self.feature_mask_fingerprint,
            "ontology_revision": self.ontology_revision,
        }


__all__ = ("PatternFormationRequest",)
