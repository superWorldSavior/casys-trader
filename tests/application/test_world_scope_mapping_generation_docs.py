from __future__ import annotations

import inspect
from typing import get_type_hints

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_scope import WorldScopeMapping
from trader.domain.world_scope_lifecycle import WorldScopeMappingGeneration


_DOC_PATHS = (
    REPO_ROOT / "docs" / "decisions" / "2026-08-25-world-scope-mapping-generation.md",
    REPO_ROOT / "docs" / "how-to" / "operate-world-model-shadow.md",
    REPO_ROOT / "docs" / "explanation" / "architecture" / "world-model-shadow.md",
    REPO_ROOT / "docs" / "reference" / "world-model.md",
)

_FORBIDDEN = (
    "invalidées en append-only (`mapping_generation_drift`)",
    "sont invalidées (`mapping_generation_drift`)",
    "invalide les cohortes pilotes encore `COLLECTING`",
    "sont invalidées en append-only",
)


def test_docs_do_not_claim_content_drift_invalidates_collecting_cohorts() -> None:
    for path in _DOC_PATHS:
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN:
            assert token not in text, f"{path}: still claims {token!r}"
        assert "COLLECTING" in text
        assert "world_scope_mapping.v1" in text


def test_mapping_generation_ports_are_consumer_owned_and_typed() -> None:
    from trader.application.world_model.scope_mapping_ports import (
        WorldScopeMappingGenerationQuery,
        WorldScopeMappingGenerationRepository,
    )

    load_hints = get_type_hints(WorldScopeMappingGenerationQuery.load_mapping_generation)
    assert list(inspect.signature(WorldScopeMappingGenerationQuery.load_mapping_generation).parameters) == [
        "self",
        "mapping_id",
        "mapping_sha256",
    ]
    assert load_hints["mapping_id"] is str
    assert load_hints["mapping_sha256"] is str
    assert load_hints["return"] == WorldScopeMappingGeneration | None

    persist_hints = get_type_hints(WorldScopeMappingGenerationRepository.persist_mapping_generation)
    assert list(inspect.signature(WorldScopeMappingGenerationRepository.persist_mapping_generation).parameters) == [
        "self",
        "mapping",
    ]
    assert persist_hints["return"] is WorldScopeMappingGeneration
    repo_load = get_type_hints(WorldScopeMappingGenerationRepository.load_mapping_generation)
    assert repo_load["return"] == WorldScopeMappingGeneration | None
    assert WorldScopeMappingGenerationQuery in WorldScopeMappingGenerationRepository.__mro__
    persist_path = REPO_ROOT / "trader" / "application" / "world_model" / "scope_mapping_ports.py"
    source = persist_path.read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.infrastructure" not in source
    assert WorldScopeMapping.__name__ in source
