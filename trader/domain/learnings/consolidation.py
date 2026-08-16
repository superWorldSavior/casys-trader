"""Politique de consolidation : identité des règles, forme du payload, keep/due.

In : notes, règles courantes, compteurs, maintenant. Out : ids stables, payload
normalisé, décisions keep/due. Invariant : pas d'I/O ; plafonds et fallbacks
de robustness s'appliquent ici.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION
from trader.domain.learnings.scoring import MEMRL_MIN_UPDATES
from trader.domain.learnings.selection import parse_ts

DEFAULT_MAX_GLOBAL = 10
DEFAULT_MAX_BY_SYMBOL = 5
VALID_ROBUSTNESS = frozenset({"pending", "low", "high"})
DEFAULT_FEEDBACK_CONSOLIDATION_THRESHOLD = 10
DEFAULT_CURATION_CATCH_UP_AGE = timedelta(days=1)
DEFAULT_RULE_GRACE_AGE = timedelta(days=3)
DEFAULT_RULE_MEASURED_UPDATES = MEMRL_MIN_UPDATES

__all__ = [
    "DEFAULT_CURATION_CATCH_UP_AGE",
    "DEFAULT_FEEDBACK_CONSOLIDATION_THRESHOLD",
    "DEFAULT_MAX_BY_SYMBOL",
    "DEFAULT_MAX_GLOBAL",
    "DEFAULT_RULE_GRACE_AGE",
    "DEFAULT_RULE_MEASURED_UPDATES",
    "VALID_ROBUSTNESS",
    "current_note_ids",
    "curation_due_reason",
    "empty_consolidated",
    "new_rule_id",
    "normalize_consolidated",
    "normalize_rule_note",
    "restore_omitted_current_rules",
    "should_keep_omitted_rule",
    "stable_rule_id",
]


def stable_rule_id(note: str) -> str:
    """Hash d'une note normalisée → ``rule_<hex16>``. Même texte = même id."""

    normalized = " ".join(str(note).lower().split())
    return f"rule_{sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


def normalize_rule_note(note: object) -> str:
    """Minuscule, espaces collapse : clé d'égalité de deux notes."""

    return " ".join(str(note or "").lower().split())


def new_rule_id(note: str, *, used_ids: set[str]) -> str:
    """``stable_rule_id(note)``, suffixé ``_2``, ``_3``, … si collision."""

    candidate = stable_rule_id(note)
    if candidate not in used_ids:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in used_ids:
        suffix += 1
    return f"{candidate}_{suffix}"


def current_note_ids(current_global: list[dict]) -> dict[str, str]:
    """Première note normalisée vue → ``rule_id`` vivant."""

    by_note: dict[str, str] = {}
    for rule in current_global:
        rule_id = str(rule.get("rule_id") or "").strip()
        note_key = normalize_rule_note(rule.get("note"))
        if rule_id and note_key and note_key not in by_note:
            by_note[note_key] = rule_id
    return by_note


def empty_consolidated() -> dict:
    """Payload vide à la version de sémantique benchmark courante."""

    return {
        "outcome_semantics_version": BENCHMARK_SEMANTICS_VERSION,
        "watermark": None,
        "global": [],
        "by_symbol": {},
    }


def _normalize_evidence_summary(value: Any) -> dict:
    """Comptes ≥ 0 et ``mean_reward`` float|None."""

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


def _normalize_entry(item: Any, *, trusted_outcome_semantics: bool) -> dict | None:
    """Garde une note non vide. Robustness ``low`` sans id explicite ou sémantique de confiance."""

    if not isinstance(item, dict):
        return None
    note = str(item.get("note") or "").strip()
    if not note:
        return None
    explicit_rule_id = str(item.get("rule_id") or "").strip()
    rule_id = explicit_rule_id or stable_rule_id(note)
    robustness = str(item.get("robustness") or "").strip().lower()
    if robustness not in VALID_ROBUSTNESS:
        robustness = "low"
    if not explicit_rule_id:
        robustness = "low"
    if not trusted_outcome_semantics:
        robustness = "low"

    evidence_note_ids: list[str] = []
    raw_evidence_ids = item.get("evidence_note_ids")
    if isinstance(raw_evidence_ids, list):
        for raw_id in raw_evidence_ids:
            evidence_id = str(raw_id).strip()
            if evidence_id and evidence_id not in evidence_note_ids:
                evidence_note_ids.append(evidence_id)

    return {
        "rule_id": rule_id,
        "note": note,
        "robustness": robustness,
        "evidence_note_ids": evidence_note_ids,
        "evidence_summary": _normalize_evidence_summary(item.get("evidence_summary")),
    }


def _normalize_entries(
    items: Any,
    *,
    limit: int,
    trusted_outcome_semantics: bool,
) -> list[dict]:
    """Au plus ``limit`` entrées valides, dans l'ordre d'apparition."""

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
    """Contrat persisté, ou None si payload / ``by_symbol`` inutilisables.

    Plafonne global et by_symbol. Id manquant → ``stable_rule_id``.
    Robustness retombe à ``low`` si invalide, sans id, ou sémantique non fiable.
    """

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


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def should_keep_omitted_rule(rule: Mapping[str, Any], *, now: datetime) -> bool:
    """True si la règle omise est déjà mesurée, ou encore dans la fenêtre de grâce."""

    memrl = rule.get("memrl") if isinstance(rule.get("memrl"), Mapping) else {}
    if _nonnegative_int(memrl.get("q_updates")) >= DEFAULT_RULE_MEASURED_UPDATES:
        return True
    created = parse_ts(memrl.get("created_at"))
    return created is not None and now - created < DEFAULT_RULE_GRACE_AGE


def _strip_memrl(rule: Mapping[str, Any]) -> dict:
    return {key: value for key, value in rule.items() if key != "memrl"}


def restore_omitted_current_rules(
    finalized: list[dict],
    current_global: list[dict],
    *,
    existing_ids: set[str],
    now: datetime,
) -> list[dict]:
    """Réinjecte les règles courantes jeunes ou mesurées omises, plafond global.

    Si le roster est plein, évince d'abord une règle dont l'id n'est pas déjà
    dans ``existing_ids``.
    """

    kept_ids = {str(rule["rule_id"]) for rule in finalized}
    omitted = [
        rule
        for rule in current_global
        if str(rule.get("rule_id") or "") not in kept_ids
        and should_keep_omitted_rule(rule, now=now)
    ]
    if not omitted:
        return finalized

    restored = list(finalized)
    for rule in omitted:
        if len(restored) < DEFAULT_MAX_GLOBAL:
            restored.append(_strip_memrl(rule))
            continue
        evict_at = next(
            (
                index
                for index, item in enumerate(restored)
                if str(item.get("rule_id") or "") not in existing_ids
            ),
            None,
        )
        if evict_at is None:
            break
        restored.pop(evict_at)
        restored.append(_strip_memrl(rule))
    return restored[:DEFAULT_MAX_GLOBAL]


def curation_due_reason(
    counts: dict[str, object] | None,
    *,
    threshold: int,
    now: datetime,
) -> str | None:
    """``new_threshold`` | ``feedback_threshold`` | ``daily_catch_up``, sinon None.

    Compteurs vides ou sans changement : pas dû. Priorité new, puis feedback, puis âge.
    """

    if not counts:
        return None
    new_count = int(counts.get("new") or 0)
    feedback_count = int(counts.get("feedback") or 0)
    if new_count >= threshold:
        return "new_threshold"
    if feedback_count >= DEFAULT_FEEDBACK_CONSOLIDATION_THRESHOLD:
        return "feedback_threshold"
    if new_count + feedback_count <= 0:
        return None
    oldest = parse_ts(counts.get("oldest_changed_at"))
    if oldest is not None and now - oldest >= DEFAULT_CURATION_CATCH_UP_AGE:
        return "daily_catch_up"
    return None
