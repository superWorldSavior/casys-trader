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


def test_ne_pousse_pas_les_flags_acceptes_dans_le_feedback_agent():
    warning = {
        "code": "risk_per_trade_exceeded",
        "field": "max_risk_per_trade_pct",
        "risk_pct": 0.015,
        "limit": 0.01,
        "context": "risk_qty=300 max_qty=200 risk_pct=0.015 limit=0.01",
    }
    store = _FakeStore([
        _row(
            "AAPL",
            "BUY",
            runtime={
                "risk_warnings": [warning],
                "tool_calls": [
                    {
                        "tool": "strategy_entry",
                        "outcome": "executed",
                        "detail": {"warnings": [warning]},
                    }
                ],
            },
        )
    ])

    r = recent_decisions_by_symbol(store, symbols=["AAPL"])["AAPL"][0]

    assert "flags" not in r


def test_compacte_les_flags_de_refus_pour_feedback_agent():
    store = _FakeStore([
        _row(
            "AAPL",
            "BUY",
            executed=False,
            reason="risk:order_value_exceeded",
            context="order_value=12000 max_order_value=10000",
            runtime={},
        )
    ])

    r = recent_decisions_by_symbol(store, symbols=["AAPL"])["AAPL"][0]

    assert r["context"] == "order_value=12000 max_order_value=10000"
    assert r["flags"] == [
        {
            "tool": "strategy_entry",
            "outcome": "blocked",
            "code": "order_value_exceeded",
            "context": "order_value=12000 max_order_value=10000",
        }
    ]


def test_compacte_flip_et_scale_in_comme_strategy_entry_sans_trace_outil():
    for intent in ("FLIP", "SCALE_IN"):
        store = _FakeStore([
            _row(
                "AAPL",
                "BUY",
                intent=intent,
                executed=False,
                reason="risk:gross_exposure_exceeded",
                runtime={},
            )
        ])

        r = recent_decisions_by_symbol(store, symbols=["AAPL"])["AAPL"][0]

        assert r["flags"][0]["tool"] == "strategy_entry"
        assert r["flags"][0]["code"] == "gross_exposure_exceeded"


def test_limit_zero_ou_negatif_ne_retourne_rien():
    # Review P2 : rows[-0:] == tout ; un limit <= 0 doit rendre {} (rien demandé).
    store = _FakeStore([_row("AAPL", "BUY"), _row("AAPL", "SELL")])
    assert recent_decisions_by_symbol(store, symbols=["AAPL"], limit=0) == {}
    assert recent_decisions_by_symbol(store, symbols=["AAPL"], limit=-1) == {}
