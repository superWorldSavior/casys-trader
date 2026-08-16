"""Inbound learnings use cases talk to outbound ports only — no agent or infra."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

from trader.agent.learnings.curation_adapter import CurationProviderAdapter, as_curation_port
from trader.application.learnings.consolidate import maybe_consolidate


class _Raw:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def all(self) -> list[dict]:
        return list(self._rows)


class _Consolidated:
    def __init__(self) -> None:
        self.payload = {
            "watermark": None,
            "global": [],
            "by_symbol": {},
            "outcome_semantics_version": 2,
        }

    def read(self) -> dict:
        return dict(self.payload)

    def write(self, payload: dict, *, watermark: str | None) -> None:
        self.payload = {**payload, "watermark": watermark}


class _Status:
    def read(self) -> dict:
        return {}

    def write_failure(self, **kwargs: object) -> None:
        return None

    def clear(self) -> None:
        return None


class _Composer:
    def compose(self, current: dict, candidates: list[dict], **kwargs: object) -> tuple[dict | None, dict | None]:
        del current, candidates, kwargs
        return {"global": [{"note": "ne pas chasser un breakout etire", "robustness": "low"}]}, None


def test_maybe_consolidate_runs_against_in_memory_ports() -> None:
    result = maybe_consolidate(
        raw=_Raw(
            [
                {
                    "ts": "2026-08-16T10:00:00+00:00",
                    "symbol": "SPY",
                    "note": "breakout etire",
                    "action": "HOLD",
                }
            ]
        ),
        consolidated=_Consolidated(),
        status=_Status(),
        composer=_Composer(),
        threshold=1,
    )
    assert result["triggered"] is True
    assert result["written"] is True


def test_application_learnings_imports_only_domain() -> None:
    root = Path(__file__).resolve().parents[2] / "trader" / "application" / "learnings"
    forbidden = (
        "trader.agent",
        "trader.infrastructure",
        "trader.runtime",
        "trader.reporting",
        "trader.market",
    )
    violations: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module == prefix or node.module.startswith(prefix + ".") for prefix in forbidden):
                    violations.append(f"{path.name}: from {node.module}")
    assert violations == []


class _Counts(Mapping):
    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class _Curation:
    def counts(self) -> Mapping[str, object]:
        return _Counts({"new": 0, "feedback": 10})

    def snapshot_pending(self) -> list[dict]:
        return []

    def select_candidates(self, *, current: dict, new_raw: list[dict]) -> list[dict]:
        del current, new_raw
        return []

    def attach_memrl(self, current: dict) -> dict:
        return current

    def acknowledge(self, **kwargs: object) -> int:
        del kwargs
        return 0

    def sync_rules(self, rule_ids: list[str]) -> dict:
        return {"active": len(rule_ids)}


def test_maybe_consolidate_accepte_counts_mapping() -> None:
    class _EmptyComposer:
        def compose(self, current: dict, candidates: list[dict], **kwargs: object):
            del current, candidates, kwargs
            return {"global": []}, None

    result = maybe_consolidate(
        raw=_Raw([]),
        consolidated=_Consolidated(),
        status=_Status(),
        composer=_EmptyComposer(),
        curation=_Curation(),
        threshold=50,
    )
    assert result["triggered"] is True
    assert result["written"] is True
    assert result["curation_due"] == "feedback_threshold"


def test_as_curation_port_passe_un_port_type() -> None:
    port = _Curation()
    assert as_curation_port(port) is port
    assert as_curation_port(None) is None


def test_as_curation_port_adapte_un_store() -> None:
    class Store:
        def curation_counts(self) -> dict:
            return {"new": 0, "feedback": 0}

    wrapped = as_curation_port(Store())
    assert isinstance(wrapped, CurationProviderAdapter)
