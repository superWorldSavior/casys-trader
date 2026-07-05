"""Décisions récentes par symbole — POUSSÉES dans les faits du cockpit (issue #4).

`last_llm_review` (déjà poussé) ne couvre que les symboles à position OUVERTE. Ici on
pousse, pour TOUT symbole décidé, ses N dernières décisions authentiques (BUY/SELL/HOLD
délibéré) — anti-répétition + cohérence, même sans position. Le row du ledger porte
~25 champs + 3 blobs ; on n'expose que l'essentiel, compacté.
"""
from __future__ import annotations

_MAX_RATIONALE_LEN = 200
DEFAULT_LIMIT = 3


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
    return {
        "cycle_ts": row.get("cycle_ts"),
        "action": row.get("action"),
        "intent": row.get("intent"),
        "confidence": row.get("confidence"),
        "decision_reason_code": row.get("decision_reason_code"),
        "executed": row.get("executed"),
        "reason": row.get("reason"),
        "rationale": rationale,
    }
