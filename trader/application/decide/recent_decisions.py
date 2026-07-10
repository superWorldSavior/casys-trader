"""Décisions récentes par symbole — POUSSÉES dans les faits du cockpit (issue #4).

`last_llm_review` (déjà poussé) ne couvre que les symboles à position OUVERTE. Ici on
pousse, pour TOUT symbole décidé, ses N dernières décisions authentiques (BUY/SELL/HOLD
délibéré) — anti-répétition + cohérence, même sans position. Le row du ledger porte
~25 champs + 3 blobs ; on n'expose que l'essentiel, compacté.
"""
from __future__ import annotations

from collections.abc import Mapping

_MAX_RATIONALE_LEN = 200
_MAX_FLAGS = 5
DEFAULT_LIMIT = 3
_ORDER_ACTION_TOOLS = {"strategy_entry", "strategy_close"}


class _LedgerReadStore:  # Protocol minimal (évite le couplage au store concret)
    def read_all(self, *, symbol: str | None = None, limit: int | None = None) -> list[dict]: ...


def recent_decisions_by_symbol(
    store: _LedgerReadStore,
    *,
    symbols: list[str],
    limit: int = DEFAULT_LIMIT,
) -> dict[str, list[dict]]:
    """`{sym: [<= limit décisions compactes, plus récent d'abord]}` pour `symbols`.

    UNE seule lecture du ledger (`read_all()` sans filtre), groupée en mémoire — pas
    N lectures. Les HOLD synthétiques (bruit infra, `llm_error != None`) sont exclus
    AVANT le tronquage, sinon ils mangeraient le budget des vraies décisions. Les
    symboles sans décision authentique sont omis du résultat.
    """
    if limit <= 0:
        return {}  # review P2 : rows[-0:] == tout — un limit nul/négatif = rien demandé.
    wanted = set(symbols)
    grouped: dict[str, list[dict]] = {sym: [] for sym in symbols}
    # read_all rend l'ordre chronologique croissant (append-only).
    for row in store.read_all():
        sym = row.get("symbol")
        if sym in wanted and not _is_synthetic_hold(row):
            grouped[sym].append(row)
    return {
        sym: [_compact(r) for r in reversed(rows[-limit:])]
        for sym, rows in grouped.items()
        if rows
    }


def annotate_learning_feedback(
    decisions_by_symbol: Mapping[str, list[dict]],
    feedback_by_decision_id: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, list[dict]]:
    """Attach feedback without changing the chronological recent-decision feed."""

    if not feedback_by_decision_id:
        return {symbol: [dict(row) for row in rows] for symbol, rows in decisions_by_symbol.items()}
    result: dict[str, list[dict]] = {}
    for symbol, rows in decisions_by_symbol.items():
        enriched: list[dict] = []
        for row in rows:
            copied = dict(row)
            decision_id = str(copied.get("decision_id") or "")
            feedback = feedback_by_decision_id.get(decision_id)
            compact_feedback = _compact_feedback(feedback)
            if compact_feedback is not None:
                copied["feedback"] = compact_feedback
            enriched.append(copied)
        result[symbol] = enriched
    return result


def _is_synthetic_hold(row: dict) -> bool:
    """HOLD synthétique = erreur LLM absorbée en HOLD infra (`llm_error` renseigné).

    Bruit à exclure : ce n'est pas une décision authentique de l'agent — le piège
    « synthetic infra HOLD » du CLAUDE.md. Une vraie décision a `llm_error is None`.
    """
    return row.get("llm_error") is not None


def _compact(row: dict) -> dict:
    rationale = row.get("rationale")
    if isinstance(rationale, str) and len(rationale) > _MAX_RATIONALE_LEN:
        rationale = rationale[:_MAX_RATIONALE_LEN] + "…"
    compact = {
        "decision_id": row.get("decision_id"),
        "cycle_ts": row.get("cycle_ts"),
        "action": row.get("action"),
        "intent": row.get("intent"),
        "confidence": row.get("confidence"),
        "decision_reason_code": row.get("decision_reason_code"),
        "executed": row.get("executed"),
        "reason": row.get("reason"),
        "rationale": rationale,
    }
    if not row.get("executed"):
        context = _decision_context(row)
        if context is not None:
            compact["context"] = context
        flags = _compact_flags(row)
        if flags:
            compact["flags"] = flags
    return compact


def _compact_feedback(value: Mapping[str, object] | None) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    status = str(value.get("status") or "pending")
    feedback = {"status": status}
    for field in ("verdict", "forward_return", "outcome_score"):
        if value.get(field) is not None:
            feedback[field] = value[field]
    return feedback


def _runtime(row: dict) -> dict:
    runtime = row.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _decision_context(row: dict) -> object | None:
    if row.get("context") is not None:
        return row.get("context")
    decision = row.get("decision")
    if isinstance(decision, dict):
        return decision.get("context")
    return None


def _reason_flag_code(reason: object) -> str | None:
    if reason is None:
        return None
    text = str(reason)
    if text in {"", "ok", "hold"}:
        return None
    return text.split(":", 1)[1] if ":" in text else text


def _flag_tool_for_row(row: dict) -> str:
    runtime = _runtime(row)
    for call in runtime.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("tool") in _ORDER_ACTION_TOOLS:
            return str(call["tool"])
    intent = str(row.get("intent") or "").upper()
    if intent in {"CLOSE", "REDUCE"}:
        return "strategy_close"
    if intent in {"FLIP", "SCALE_IN"}:
        return "strategy_entry"
    if row.get("action") in {"BUY", "SELL"}:
        return "strategy_entry"
    return "decision"


def _compact_flags(row: dict) -> list[dict]:
    runtime = _runtime(row)
    flags: list[dict] = []
    seen: set[tuple[object, object, object]] = set()

    def add_flag(*, tool: str, outcome: object, warning: object) -> None:
        if not isinstance(warning, dict):
            return
        code = warning.get("code")
        key = (tool, outcome, code)
        if key in seen:
            return
        seen.add(key)
        flag = {
            "tool": tool,
            "outcome": outcome,
            "code": code,
        }
        for field in ("field", "risk_pct", "limit", "distance_pct", "limit_pct", "context"):
            if warning.get(field) is not None:
                flag[field] = warning[field]
        flags.append(flag)

    for call in runtime.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        detail = call.get("detail")
        if not isinstance(detail, dict):
            continue
        for warning in detail.get("warnings") or []:
            add_flag(tool=str(call.get("tool") or "tool"), outcome=call.get("outcome"), warning=warning)

    for warning in runtime.get("risk_warnings") or []:
        add_flag(tool=_flag_tool_for_row(row), outcome="executed" if row.get("executed") else "blocked", warning=warning)

    for warning in runtime.get("exit_plan_warnings") or []:
        add_flag(tool=_flag_tool_for_row(row), outcome="executed" if row.get("executed") else "blocked", warning=warning)

    if not row.get("executed"):
        code = _reason_flag_code(row.get("reason"))
        if code is not None and row.get("action") in {"BUY", "SELL"}:
            add_flag(
                tool=_flag_tool_for_row(row),
                outcome="blocked",
                warning={"code": code, "context": _decision_context(row)},
            )

    return flags[:_MAX_FLAGS]
