"""Map a LearningsStore (or test fake) onto CurationPort.

A value that already exposes the CurationPort methods is passed through
(``as_curation_port``). Only store-shaped objects are wrapped.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from trader.domain.learnings.curation import candidate_id

log = logging.getLogger(__name__)

__all__ = ["CurationProviderAdapter", "as_curation_port"]

_PORT_METHODS = (
    "counts",
    "snapshot_pending",
    "select_candidates",
    "attach_memrl",
    "acknowledge",
    "sync_rules",
)


def as_curation_port(provider: object | None):
    """Return ``None``, the provider itself if it is a CurationPort, or an adapter."""

    if provider is None:
        return None
    if all(callable(getattr(provider, name, None)) for name in _PORT_METHODS):
        return provider
    return CurationProviderAdapter(provider)


class CurationProviderAdapter:
    """Map a LearningsStore (or test fake) onto CurationPort."""

    def __init__(self, provider: object) -> None:
        self._provider = provider

    def counts(self) -> Mapping[str, object] | None:
        counter = getattr(self._provider, "curation_counts", None)
        if not callable(counter):
            return None
        payload = counter()
        return payload if isinstance(payload, Mapping) else None

    def snapshot_pending(self) -> list[dict] | None:
        snapshot = getattr(self._provider, "pending_curation_snapshots", None)
        if not callable(snapshot):
            return None
        rows = snapshot()
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TypeError("pending_curation_snapshots must return list[dict]")
        return rows

    def select_candidates(self, *, current: dict, new_raw: list[dict]) -> list[dict]:
        selector = getattr(self._provider, "select_candidates", None) or getattr(
            self._provider, "select_curation_candidates", None
        )
        if not callable(selector) and callable(self._provider):
            selector = self._provider
        if not callable(selector):
            raise TypeError("curation_provider must expose select_candidates(...) or be callable")
        try:
            rows = selector(current=current, new_raw=new_raw)
        except TypeError:
            rows = selector()
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TypeError("curation_provider must return list[dict]")
        return rows

    def attach_memrl(self, current: dict) -> dict:
        reader = getattr(self._provider, "global_rule_scores", None)
        if not callable(reader):
            return current
        rule_ids = [
            str(rule.get("rule_id"))
            for rule in current.get("global", [])
            if isinstance(rule, dict) and rule.get("rule_id")
        ]
        if not rule_ids:
            return current
        scores = reader(rule_ids)
        if not isinstance(scores, Mapping):
            return current
        rules: list[dict] = []
        for rule in current.get("global", []):
            if not isinstance(rule, dict):
                continue
            copied = dict(rule)
            score = scores.get(str(copied.get("rule_id") or ""))
            if isinstance(score, dict):
                q_value = float(score.get("q_value") or 0.0)
                q_updates = max(0, int(score.get("q_updates") or 0))
                copied["memrl"] = {
                    "q_value": q_value,
                    "q_updates": q_updates,
                    "shrunk_q": q_value * q_updates / (q_updates + 5.0),
                    "created_at": score.get("created_at"),
                }
            rules.append(copied)
        return {**current, "global": rules}

    def acknowledge(
        self,
        *,
        candidates: Sequence[dict],
        source_rows: Sequence[dict] | None,
        watermark: str | None,
    ) -> int:
        store_marker = getattr(self._provider, "mark_curation_candidates_curated", None)
        if callable(store_marker):
            candidate_ids = {str(candidate["id"]) for candidate in candidates if candidate.get("id")}
            candidate_decision_ids = {
                str(candidate.get("decision_id") or "")
                for candidate in candidates
                if candidate.get("decision_id")
            }
            source_subset = [
                row
                for index, row in enumerate(source_rows or [])
                if (
                    candidate_id(row, index=index) in candidate_ids
                    or str(row.get("decision_id") or "") in candidate_decision_ids
                )
            ]
            try:
                result = store_marker(source_subset)
            except Exception as exc:  # pragma: no cover - defensive integration seam
                log.warning("unable to mark curated learning candidates (%s)", exc)
                return 0
            return int(result) if isinstance(result, int) else len(source_subset)

        marker = getattr(self._provider, "mark_candidates_curated", None) or getattr(
            self._provider, "mark_curated", None
        )
        if not callable(marker):
            return 0
        candidate_ids = [str(candidate["id"]) for candidate in candidates if candidate.get("id")]
        try:
            result = marker(candidate_ids=candidate_ids, watermark=watermark)
        except TypeError:
            try:
                result = marker(candidate_ids)
            except Exception as exc:  # pragma: no cover - defensive integration seam
                log.warning("unable to mark curated learning candidates (%s)", exc)
                return 0
        except Exception as exc:  # pragma: no cover - defensive integration seam
            log.warning("unable to mark curated learning candidates (%s)", exc)
            return 0
        return int(result) if isinstance(result, int) else len(candidate_ids)

    def sync_rules(self, rule_ids: Sequence[str]) -> dict | None:
        sync = getattr(self._provider, "sync_global_rules", None)
        if not callable(sync):
            return None
        try:
            result = sync(list(rule_ids))
        except Exception as exc:  # pragma: no cover - defensive integration seam
            log.warning("unable to synchronize active global rules (%s)", exc)
            return None
        return result if isinstance(result, dict) else {"active": len(rule_ids)}
