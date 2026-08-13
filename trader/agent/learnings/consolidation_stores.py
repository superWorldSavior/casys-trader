"""JSON stores and normalization helpers for consolidated learnings."""

from __future__ import annotations

import json
import logging
import os
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION

log = logging.getLogger("trader.agent.learnings.consolidator")

DEFAULT_MAX_GLOBAL = 10
DEFAULT_MAX_BY_SYMBOL = 5
VALID_ROBUSTNESS = frozenset({"pending", "low", "high"})


def stable_rule_id(note: str) -> str:
    """Return the deterministic id used to migrate legacy global rules.

    IDs created by the consolidator for genuinely new rules are also derived from
    the rule text.  A retained or reformulated rule must instead carry its
    existing ID in the LLM output; this helper only provides the safe fallback
    for legacy files and new, unlabelled rules.
    """

    normalized = " ".join(str(note).lower().split())
    return f"rule_{sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


def empty_consolidated() -> dict:
    return {
        "outcome_semantics_version": BENCHMARK_SEMANTICS_VERSION,
        "watermark": None,
        "global": [],
        "by_symbol": {},
    }


def _normalize_entry(item: Any, *, trusted_outcome_semantics: bool) -> dict | None:
    if not isinstance(item, dict):
        return None
    note = str(item.get("note") or "").strip()
    if not note:
        return None
    explicit_rule_id = str(item.get("rule_id") or "").strip()
    rule_id = explicit_rule_id or stable_rule_id(note)
    robustness = str(item.get("robustness") or "").strip().lower()
    # Legacy rules have neither provenance nor a measured robustness.  They are
    # still readable, but must not masquerade as a validated rule.
    if robustness not in VALID_ROBUSTNESS:
        robustness = "low"
    # A legacy rule has no stable provenance.  It remains usable after the
    # migration, but is never presented as a high-confidence conclusion until
    # the outcome-weighted consolidator has sourced it again.
    if not explicit_rule_id:
        robustness = "low"
    # Rules scored before the benchmark-v2 correction may have promoted an
    # abstention simply because the market later fell.  Keep the useful text
    # and provenance, but fail closed on confidence until a v2 consolidation
    # evaluates the evidence again.
    if not trusted_outcome_semantics:
        robustness = "low"

    evidence_note_ids: list[str] = []
    raw_evidence_ids = item.get("evidence_note_ids")
    if isinstance(raw_evidence_ids, list):
        for raw_id in raw_evidence_ids:
            evidence_id = str(raw_id).strip()
            if evidence_id and evidence_id not in evidence_note_ids:
                evidence_note_ids.append(evidence_id)

    entry = {
        "rule_id": rule_id,
        "note": note,
        "robustness": robustness,
        "evidence_note_ids": evidence_note_ids,
        "evidence_summary": _normalize_evidence_summary(item.get("evidence_summary")),
    }
    return entry


def _migrate_legacy_global_rules(payload: dict) -> bool:
    """Persist stable IDs without retiring legacy ``by_symbol`` entries yet."""

    raw_global = payload.get("global")
    if not isinstance(raw_global, list):
        return False
    changed = False
    for item in raw_global:
        if not isinstance(item, dict):
            continue
        note = str(item.get("note") or "").strip()
        if not note or str(item.get("rule_id") or "").strip():
            continue
        item["rule_id"] = stable_rule_id(note)
        # Old rules did not carry evidence; force an honest provisional status.
        item["robustness"] = "low"
        item.setdefault("evidence_note_ids", [])
        changed = True
    return changed


def _normalize_evidence_summary(value: Any) -> dict:
    """Keep only the machine-owned evidence summary shape.

    Values are calculated by the consolidator before the payload is persisted.
    This normalizer makes historical files safe to read and deliberately drops
    any extra model-authored prose/fields.
    """

    raw = value if isinstance(value, dict) else {}

    def count(name: str) -> int:
        try:
            return max(0, int(raw.get(name, 0)))
        except (TypeError, ValueError):
            return 0

    reward = raw.get("mean_reward")
    try:
        mean_reward = float(reward) if reward is not None else None
    except (TypeError, ValueError):
        mean_reward = None
    return {
        "evaluated": count("evaluated"),
        "pending": count("pending"),
        "wins": count("wins"),
        "losses": count("losses"),
        "neutrals": count("neutrals"),
        "mean_reward": mean_reward,
    }


def _normalize_entries(
    items: Any,
    *,
    limit: int,
    trusted_outcome_semantics: bool,
) -> list[dict]:
    if not isinstance(items, list):
        return []
    entries: list[dict] = []
    for item in items:
        entry = _normalize_entry(
            item,
            trusted_outcome_semantics=trusted_outcome_semantics,
        )
        if entry is not None:
            entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def normalize_consolidated(payload: Any, *, watermark: str | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    by_symbol_raw = payload.get("by_symbol", {})
    if not isinstance(by_symbol_raw, dict):
        return None

    raw_semantics_version = payload.get("outcome_semantics_version")
    semantics_version = (
        raw_semantics_version
        if isinstance(raw_semantics_version, int) and not isinstance(raw_semantics_version, bool)
        else None
    )
    trusted_outcome_semantics = semantics_version == BENCHMARK_SEMANTICS_VERSION

    by_symbol: dict[str, list[dict]] = {}
    for raw_symbol, items in by_symbol_raw.items():
        symbol = str(raw_symbol).strip()
        if not symbol:
            continue
        entries = _normalize_entries(
            items,
            limit=DEFAULT_MAX_BY_SYMBOL,
            trusted_outcome_semantics=trusted_outcome_semantics,
        )
        if entries:
            by_symbol[symbol] = entries

    return {
        "outcome_semantics_version": semantics_version,
        "watermark": watermark,
        "global": _normalize_entries(
            payload.get("global", []),
            limit=DEFAULT_MAX_GLOBAL,
            trusted_outcome_semantics=trusted_outcome_semantics,
        ),
        "by_symbol": by_symbol,
    }


class ConsolidatedLearningsStore:
    def __init__(self, path: str | Path, *, history_path: str | Path | None = None):
        self.path = Path(path)
        self.history_path = (
            Path(history_path)
            if history_path is not None
            else self.path.parent / "archive" / f"{self.path.stem}-history.jsonl"
        )

    def read(self) -> dict:
        if not self.path.exists():
            return empty_consolidated()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return empty_consolidated()
        if isinstance(payload, dict) and _migrate_legacy_global_rules(payload):
            # This narrow migration deliberately preserves legacy by_symbol
            # data.  It is cleared only by the next successful consolidation.
            try:
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:
                log.warning("migration des rule_ids non ecrite %s (%s)", self.path, exc)
        normalized = normalize_consolidated(payload, watermark=payload.get("watermark"))
        return normalized or empty_consolidated()

    def write(self, payload: dict, *, watermark: str | None) -> None:
        normalized = normalize_consolidated(payload, watermark=watermark)
        if normalized is None:
            raise ValueError("invalid consolidated learnings payload")
        # `by_symbol` remains readable in legacy files, but every newly written
        # consolidated payload retires it.  Situation/continuity belongs in the
        # decision context, not in a stale per-symbol learning digest.
        normalized["by_symbol"] = {}
        self._archive_replaced(replaced_by_watermark=normalized.get("watermark"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _archive_replaced(self, *, replaced_by_watermark: str | None) -> None:
        if not self.path.exists():
            return
        try:
            previous = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        entry = {
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "replaced_by_watermark": replaced_by_watermark,
            "payload": previous,
        }
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("historisation consolide non ecrite %s (%s)", self.history_path, exc)


class ConsolidationStatusStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_failure(
        self,
        *,
        consolidated_watermark: str | None,
        raw_watermark: str | None,
        new_raw_count: int,
        error: dict,
    ) -> None:
        payload = {
            "last_failure": {
                "consolidated_watermark": consolidated_watermark,
                "raw_watermark": raw_watermark,
                "new_raw_count": new_raw_count,
                "error_code": str(error.get("error_code") or "unknown"),
                "error_message": str(error.get("error_message") or "")[:500],
                "provider": error.get("provider"),
                "model": error.get("model"),
            }
        }
        optional_fields = (
            "requested_model",
            "output_preview",
            "output_tail",
            "output_length",
        )
        for field in optional_fields:
            if field in error:
                payload["last_failure"][field] = error[field]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
