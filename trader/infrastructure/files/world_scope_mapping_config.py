"""Atomic YAML adapter for the operator-committed WorldScopeMapping generation."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_scope import WORLD_SCOPE_MAPPING_SCHEMA, WorldScopeMapping


_OPERATOR_DEFAULTS: dict[str, Any] = {
    "taxonomy_version": "sessions_mic.v1",
    "unmapped_policy": "admit_as_missing_status_only",
    "ambiguous_policy": "admit_as_missing_status_only",
    "no_logical_venue_mic_fallback": True,
}


def mapping_config_path(path: str | Path) -> Path:
    resolved = Path(path)
    if resolved.is_dir():
        return resolved / "world_scope_mapping.yaml"
    return resolved


class YamlWorldScopeMappingStore:
    def __init__(self, path: str | Path) -> None:
        self._path = mapping_config_path(path)

    def load(self) -> WorldScopeMapping:
        return WorldScopeResolver.load(self._path).mapping

    def save(self, mapping: WorldScopeMapping) -> None:
        if not isinstance(mapping, WorldScopeMapping):
            raise TypeError("mapping must be WorldScopeMapping")
        if mapping.mapping_id != WORLD_SCOPE_MAPPING_SCHEMA:
            raise ValueError(f"mapping_id must be {WORLD_SCOPE_MAPPING_SCHEMA}")
        existing = _load_raw(self._path)
        payload = operator_mapping_payload(mapping, existing)
        _atomic_yaml_write(self._path, payload)


def operator_mapping_payload(
    mapping: WorldScopeMapping,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    extras = dict(existing or ())
    payload: dict[str, Any] = {
        "schema_version": mapping.schema_version,
        "mapping_id": mapping.mapping_id,
        "taxonomy_version": extras.get("taxonomy_version") or _OPERATOR_DEFAULTS["taxonomy_version"],
        "content_sha256": mapping.content_sha256,
        "unmapped_policy": extras.get("unmapped_policy") or _OPERATOR_DEFAULTS["unmapped_policy"],
        "ambiguous_policy": extras.get("ambiguous_policy") or _OPERATOR_DEFAULTS["ambiguous_policy"],
        "no_logical_venue_mic_fallback": (
            extras.get("no_logical_venue_mic_fallback")
            if "no_logical_venue_mic_fallback" in extras
            else _OPERATOR_DEFAULTS["no_logical_venue_mic_fallback"]
        ),
        "entries": [entry.to_dict() for entry in mapping.entries],
    }
    payload["operator_config_sha256"] = canonical_sha256(payload)
    return payload


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(payload) if isinstance(payload, Mapping) else {}


def _atomic_yaml_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(payload), handle, default_flow_style=False, sort_keys=False, allow_unicode=True)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


__all__ = [
    "YamlWorldScopeMappingStore",
    "mapping_config_path",
    "operator_mapping_payload",
]
