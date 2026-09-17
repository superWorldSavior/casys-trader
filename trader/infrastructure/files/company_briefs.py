"""Read-only adapter over the mutable company-intelligence current state.

Each ``current/*.json`` file holds one symbol with ``screen`` and/or ``deep``
briefs. This loader picks the richest available brief per symbol
(deep-first, screen fallback) and parses it into a
``CompanyIntelligenceBrief``. Unreadable files are skipped and counted, never
silent: derivation honesty requires knowing what was dropped.

Point-in-time note: ``current/`` is latest-wins mutable state, not an
append-only ledger. Registry identities built from it are stable across
refreshes (hash covers identities only), but strict historical
reproducibility would need an archived brief generation per revision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trader.domain.company.intelligence import CompanyIntelligenceBrief


@dataclass(frozen=True)
class CompanyBriefCorpus:
    briefs: Mapping[str, CompanyIntelligenceBrief]
    skipped_files: tuple[str, ...]

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_files)


def _pick_brief(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    briefs = payload.get("briefs")
    if not isinstance(briefs, Mapping):
        return None
    for depth in ("deep", "screen"):
        candidate = briefs.get(depth)
        if isinstance(candidate, Mapping):
            return candidate
    return None


def load_company_brief_file(path: str | Path) -> CompanyIntelligenceBrief | None:
    """Parse one current-state brief file. None on any problem, never raises."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    raw = _pick_brief(payload)
    if raw is None:
        return None
    try:
        brief = CompanyIntelligenceBrief.from_mapping(raw)
    except (TypeError, ValueError):
        return None
    if brief is None or not brief.symbol:
        return None
    return brief


def load_company_briefs(root: str | Path) -> CompanyBriefCorpus:
    """Load one parsed brief per symbol. Never writes, never raises on rows."""

    base = Path(root)
    current = base if base.name == "current" else base / "current"
    briefs: dict[str, CompanyIntelligenceBrief] = {}
    skipped: list[str] = []
    if not current.is_dir():
        return CompanyBriefCorpus(briefs=briefs, skipped_files=())
    for path in sorted(current.glob("*.json")):
        brief = load_company_brief_file(path)
        if brief is None or brief.symbol in briefs:
            skipped.append(path.name)
            continue
        briefs[brief.symbol] = brief
    return CompanyBriefCorpus(briefs=briefs, skipped_files=tuple(skipped))


__all__ = [
    "CompanyBriefCorpus",
    "load_company_brief_file",
    "load_company_briefs",
]
