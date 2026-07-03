"""Agrégats purs pour la home du cockpit.

Aucune I/O, aucun import Textual — tout est calculé depuis le dict d'état
du read model (`load_runtime_state`). Testable sans UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trader.read_models.runtime_state import _safe_float, _safe_list_of_dicts

UTC = timezone.utc


def parse_ts(raw: object) -> datetime | None:
    """ISO 8601 (suffixe Z accepté) → datetime UTC-aware, sinon None."""
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decision_status(row: dict) -> str:
    """Classe une décision (logique historique de overview._decision_status)."""
    reason = str(row.get("reason") or row.get("rationale") or "").lower()
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
    source = str(row.get("decision_source") or "").lower()
    action = str(row.get("action") or "").upper()
    if row.get("executed") is True:
        return "exec"
    if reason.startswith("risk:") or "risk:" in reason:
        return "risk"
    if "stale" in reason:
        return "stale"
    if source == "armed_plan" or runtime.get("armed_plan_id"):
        return "armé"
    if runtime.get("trade_plan_created"):
        return "plan"
    if runtime.get("indicator_watch_created"):
        return "veille"
    if source == "infra" or (row.get("model_called") is False and "quiet" in reason):
        return "quiet"
    return "hold" if action == "HOLD" else "signal"


def decision_priority(row: dict) -> tuple[int, str]:
    """Score de triage (0 = plus urgent) + ts pour départager."""
    action = str(row.get("action") or "").upper()
    status = decision_status(row)
    score = 90
    if row.get("executed") is True and action in {"BUY", "SELL"}:
        score = 0
    elif action in {"BUY", "SELL"} and status in {"risk", "stale"}:
        score = 10
    elif status in {"risk", "stale"}:
        score = 20
    elif status in {"plan", "veille", "armé"}:
        score = 30
    elif action in {"BUY", "SELL"}:
        score = 40
    elif status == "quiet":
        score = 80
    return score, str(row.get("cycle_ts") or row.get("ts") or "")


def _decision_identity(row: dict) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("cycle_ts") or row.get("ts") or ""),
        str(row.get("sequence") or ""),
        str(row.get("symbol") or ""),
        str(row.get("action") or ""),
        str(row.get("reason") or row.get("rationale") or ""),
    )


def decision_source_rows(decisions: list[dict], recent_decisions: list[dict]) -> list[dict]:
    """Fusionne rapport + tail decisions.jsonl en dédupliquant par identité."""
    rows: list[dict] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in [*recent_decisions, *decisions]:
        identity = _decision_identity(row)
        if identity in seen:
            continue
        rows.append(row)
        seen.add(identity)
    return rows


def select_decision_rows(
    decisions: list[dict], recent_decisions: list[dict], *, limit: int
) -> list[dict]:
    """Top-N décisions par priorité (les plus récentes d'abord à score égal)."""
    indexed = list(enumerate(decision_source_rows(decisions, recent_decisions)))
    ranked = sorted(indexed, key=lambda item: (decision_priority(item[1])[0], -item[0]))
    return [row for _, row in ranked[:limit]]
