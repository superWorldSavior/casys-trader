import pytest

from trader.reporting.decision_reason import infer_reason_code


def test_decision_reason_domain_canonical_reporting_compat() -> None:
    from trader.domain import decision_reason as canonical
    from trader.reporting import decision_reason as legacy

    assert canonical.infer_reason_code is legacy.infer_reason_code
    assert canonical.normalize_reason_code is legacy.normalize_reason_code
    assert canonical.reason_code_enum_text() == legacy.reason_code_enum_text()


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"action": "HOLD", "rationale": "range neutre"}, "NO_EDGE"),
        ({"action": "HOLD", "rationale": "attendre un pullback propre"}, "WAITING_PULLBACK"),
        ({"action": "HOLD", "rationale": "frais et commissions trop élevés"}, "FEES_TOO_HIGH"),
        ({"action": "HOLD", "reason": "stale_market_data"}, "DATA_STALE"),
        ({"action": "HOLD", "rationale": "market closed"}, "MARKET_CLOSED"),
        ({"action": "HOLD", "reason": "risk:max_exposure"}, "RISK_LIMIT"),
        ({"action": "HOLD", "rationale": "already exposed on the same theme"}, "ALREADY_EXPOSED"),
        ({"action": "HOLD", "rationale": "mixed signals"}, "CONFLICTING_SIGNALS"),
        ({"action": "HOLD", "runtime": {"indicator_watch_created": True}}, "WATCH_ARMED"),
        ({"action": "HOLD", "rationale": "post-loss prudence"}, "POST_LOSS_CAUTION"),
        ({"action": "HOLD", "rationale": "position existante à conserver"}, "POSITION_MANAGEMENT"),
        ({"action": "BUY", "intent": "OPEN_LONG", "rationale": "cassure exploitable"}, "ENTRY_SIGNAL"),
        ({"action": "SELL", "intent": "CLOSE", "rationale": "sortir sur invalidation"}, "EXIT_SIGNAL"),
        ({"action": "HOLD", "decision_source": "armed_plan"}, "ARMED_PLAN"),
        ({"action": "REQUEST_CONTEXT", "rationale": "besoin contexte"}, "UNKNOWN"),
    ],
)
def test_infer_reason_code_fallback_legacy_codes_directs(row: dict, expected: str) -> None:
    assert infer_reason_code(row) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"action": "HOLD", "reason": "risk:max_exposure", "decision_source": "armed_plan"}, "RISK_LIMIT"),
        (
            {"action": "HOLD", "decision_source": "armed_plan", "reason": "stale_market_data"},
            "ARMED_PLAN",
        ),
        ({"action": "HOLD", "reason": "stale_market_data", "rationale": "market closed"}, "DATA_STALE"),
        (
            {"action": "HOLD", "rationale": "market closed, veille posée", "runtime": {"indicator_watch_created": True}},
            "MARKET_CLOSED",
        ),
        (
            {"action": "BUY", "intent": "OPEN_LONG", "runtime": {"indicator_watch_created": True}},
            "WATCH_ARMED",
        ),
        ({"action": "SELL", "intent": "CLOSE", "rationale": "loss et sortir"}, "EXIT_SIGNAL"),
        ({"action": "HOLD", "rationale": "loss mais frais élevés"}, "POST_LOSS_CAUTION"),
        ({"action": "HOLD", "rationale": "frais sur position existante"}, "FEES_TOO_HIGH"),
        ({"action": "HOLD", "rationale": "already exposed avec position existante"}, "ALREADY_EXPOSED"),
        ({"action": "HOLD", "rationale": "position à conserver, attendre pullback"}, "POSITION_MANAGEMENT"),
        ({"action": "HOLD", "rationale": "attendre pullback malgré signaux mixed"}, "WAITING_PULLBACK"),
        ({"action": "HOLD", "rationale": "mixed signals, pas de confluence"}, "CONFLICTING_SIGNALS"),
    ],
)
def test_infer_reason_code_fallback_legacy_priorites_overlap(row: dict, expected: str) -> None:
    assert infer_reason_code(row) == expected
