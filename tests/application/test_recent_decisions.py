"""Tests — recent_decisions_by_symbol (issue #4, push) : groupage, filtre, compactage."""
from trader.application.recent_decisions import recent_decisions_by_symbol


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows
        self.read_calls = 0

    def read_all(self, *, symbol=None, limit=None):
        self.read_calls += 1
        rows = [r for r in self._rows if symbol is None or r.get("symbol") == symbol]
        if limit is not None and limit >= 0:
            rows = rows[-limit:]
        return rows


def _row(symbol, action, rationale="ok", **over):
    base = {
        "symbol": symbol, "cycle_ts": f"t-{action}", "action": action,
        "intent": action, "confidence": 0.7, "decision_reason_code": "LLM_SIGNAL",
        "executed": True, "reason": None, "llm_error": None, "rationale": rationale,
        "decision": {"huge": "blob"}, "market_snapshot": {"x": 1}, "portfolio_snapshot": {"y": 2},
    }
    base.update(over)
    return base


def test_groupe_par_symbole_en_une_seule_lecture():
    store = _FakeStore([_row("AAPL", "BUY"), _row("MSFT", "SELL"), _row("AAPL", "HOLD")])

    out = recent_decisions_by_symbol(store, symbols=["AAPL", "MSFT"])

    assert store.read_calls == 1  # une lecture, pas une par symbole
    assert {r["action"] for r in out["AAPL"]} == {"BUY", "HOLD"}
    assert [r["action"] for r in out["MSFT"]] == ["SELL"]


def test_plus_recent_dabord_et_limite():
    store = _FakeStore([_row("AAPL", "A"), _row("AAPL", "B"), _row("AAPL", "C")])

    out = recent_decisions_by_symbol(store, symbols=["AAPL"], limit=2)

    assert [r["action"] for r in out["AAPL"]] == ["C", "B"]  # 2 plus récentes, récent d'abord


def test_exclut_hold_synthetiques_sans_manger_le_budget():
    store = _FakeStore([
        _row("AAPL", "BUY", llm_error=None),
        _row("AAPL", "HOLD", llm_error="timeout"),
        _row("AAPL", "HOLD", llm_error="rate_limited"),
        _row("AAPL", "SELL", llm_error=None),
    ])

    out = recent_decisions_by_symbol(store, symbols=["AAPL"], limit=2)

    assert [r["action"] for r in out["AAPL"]] == ["SELL", "BUY"]


def test_symbole_sans_decision_est_omis():
    store = _FakeStore([_row("AAPL", "BUY")])

    out = recent_decisions_by_symbol(store, symbols=["AAPL", "MSFT"])

    assert "AAPL" in out
    assert "MSFT" not in out  # pas de clé vide


def test_ne_fuit_pas_les_blobs_et_tronque_le_rationale():
    store = _FakeStore([_row("AAPL", "BUY", rationale="x" * 500)])

    r = recent_decisions_by_symbol(store, symbols=["AAPL"])["AAPL"][0]

    assert "decision" not in r and "market_snapshot" not in r
    assert len(r["rationale"]) <= 201 and r["rationale"].endswith("…")
