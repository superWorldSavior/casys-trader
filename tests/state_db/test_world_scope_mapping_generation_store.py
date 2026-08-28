from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)
from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore


def _entry(instrument: str) -> WorldScopeMappingEntry:
    return WorldScopeMappingEntry(
        anchor=WorldMarketAnchorRef(market_venue="US", instrument=instrument),
        venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XNYS"),
        country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:US"),
        region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:021"),
        world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
        provider_proofs=("provider:listing",),
        taxonomy_version="sessions_mic.v1",
    )


def _mapping(*instruments: str) -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=tuple(_entry(item) for item in instruments),
    )


def test_mapping_generation_persist_is_idempotent_and_survives_restart(tmp_path: Path) -> None:
    mapping = _mapping("GM")
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path)
    try:
        first = store.persist_mapping_generation(mapping)
        second = store.persist_mapping_generation(mapping)
        assert first.mapping_sha256 == mapping.content_sha256 == second.mapping_sha256
        loaded = store.load_mapping_generation(mapping.mapping_id, mapping.content_sha256)
        assert loaded is not None
        assert loaded.mapping_sha256 == mapping.content_sha256
        assert loaded.contains_anchor(WorldMarketAnchorRef(market_venue="US", instrument="GM"))
    finally:
        store.close()

    restarted = WorldModelStore(path)
    try:
        loaded = restarted.load_mapping_generation(mapping.mapping_id, mapping.content_sha256)
        assert loaded is not None
        assert loaded.mapping.to_dict() == mapping.to_dict()
        later = _mapping("GM", "REGN")
        restarted.persist_mapping_generation(later)
        assert restarted.load_mapping_generation(mapping.mapping_id, mapping.content_sha256) is not None
        assert restarted.load_mapping_generation(later.mapping_id, later.content_sha256) is not None
        assert mapping.content_sha256 != later.content_sha256
    finally:
        restarted.close()


def test_divergent_payload_for_same_mapping_generation_identity_conflicts(tmp_path: Path) -> None:
    mapping = _mapping("GM")
    store = WorldModelStore(tmp_path / "world_model.db")
    try:
        with store._db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO world_scope_mapping_generations(
                    mapping_id, mapping_sha256, payload_json, payload_sha256, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    mapping.mapping_id,
                    mapping.content_sha256,
                    '{"tampered":true}',
                    "0" * 64,
                    "2026-08-24T00:00:00+00:00",
                ),
            )
        with pytest.raises(WorldModelConflictError, match="different canonical content"):
            store.persist_mapping_generation(mapping)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store._db.transaction() as cur:
                cur.execute("UPDATE world_scope_mapping_generations SET payload_json='rewritten'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store._db.transaction() as cur:
                cur.execute("DELETE FROM world_scope_mapping_generations")
    finally:
        store.close()
