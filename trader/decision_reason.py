"""Reason-code helpers for auditable agent decisions."""

from __future__ import annotations

from typing import Any

REASON_CODES = (
    "NO_EDGE",
    "WAITING_PULLBACK",
    "FEES_TOO_HIGH",
    "DATA_STALE",
    "MARKET_CLOSED",
    "RISK_LIMIT",
    "ALREADY_EXPOSED",
    "CONFLICTING_SIGNALS",
    "WATCH_ARMED",
    "POST_LOSS_CAUTION",
    "POSITION_MANAGEMENT",
    "ENTRY_SIGNAL",
    "EXIT_SIGNAL",
    "ARMED_PLAN",
    "UNKNOWN",
)

_REASON_CODE_SET = set(REASON_CODES)


def normalize_reason_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return code if code in _REASON_CODE_SET else "UNKNOWN"


def _text_blob(row: dict) -> str:
    parts = [
        row.get("reason"),
        row.get("rationale"),
        row.get("learning"),
    ]
    decision = row.get("decision")
    if isinstance(decision, dict):
        parts.extend([decision.get("rationale"), decision.get("learning")])
    return " ".join(str(part or "") for part in parts).lower()


def infer_reason_code(row: dict) -> str:
    """Returns explicit reason code, or a deterministic legacy fallback."""
    explicit = normalize_reason_code(row.get("decision_reason_code"))
    if explicit != "UNKNOWN":
        return explicit
    decision = row.get("decision")
    if isinstance(decision, dict):
        nested = normalize_reason_code(decision.get("decision_reason_code"))
        if nested != "UNKNOWN":
            return nested

    action = str(row.get("action") or "").upper()
    intent = str(row.get("intent") or "").upper()
    reason = str(row.get("reason") or "").lower()
    source = str(row.get("decision_source") or "").lower()
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
    text = _text_blob(row)

    if reason.startswith("risk:") or "confidence_below_required" in reason:
        return "RISK_LIMIT"
    if source == "armed_plan" or runtime.get("armed_plan_id") or text.startswith("armed_plan:"):
        return "ARMED_PLAN"
    if reason == "stale_market_data" or "runtime_stale" in text or "données stale" in text or "donnees stale" in text:
        return "DATA_STALE"
    if "marché fermé" in text or "marche ferme" in text or "market closed" in text or "session ferm" in text:
        return "MARKET_CLOSED"
    if runtime.get("indicator_watch_created") or runtime.get("indicator_watch_requested") or "veille" in text or "watch" in text:
        return "WATCH_ARMED"
    if action in {"BUY", "SELL"}:
        if intent in {"CLOSE", "REDUCE"} or "invalidation" in text or "sortir" in text:
            return "EXIT_SIGNAL"
        if intent in {"OPEN_LONG", "OPEN_SHORT"}:
            return "ENTRY_SIGNAL"
    if "hard_stop" in text or "perte" in text or "loss" in text or "post-loss" in text:
        return "POST_LOSS_CAUTION"
    if "frais" in text or "fee" in text or "commission" in text or "be_ref" in text or "coût" in text or "cout" in text:
        return "FEES_TOO_HIGH"
    if "already exposed" in text or "déjà expos" in text or "deja expos" in text:
        return "ALREADY_EXPOSED"
    if "position" in text or "conserver" in text or "renforcer" in text or "reduce" in intent:
        return "POSITION_MANAGEMENT"
    if "pullback" in text or "retest" in text or "digestion" in text or "attendre" in text:
        return "WAITING_PULLBACK"
    if "contradic" in text or "divergence" in text or "pas de confluence" in text or "mixed" in text:
        return "CONFLICTING_SIGNALS"
    if action == "HOLD":
        return "NO_EDGE"
    return "UNKNOWN"


def reason_code_enum_text() -> str:
    return "|".join(REASON_CODES)
