from trader.application.gross_feedback import summarize_gross_rejections


def test_summarize_gross_rejections_reports_rejected_opening_symbols() -> None:
    decisions = [
        {"symbol": "B", "intent": "OPEN_SHORT", "reason": "risk:gross_exposure_exceeded"},
        {"symbol": "A", "intent": "SCALE_IN", "reason": "risk:gross_exposure_exceeded"},
        {"symbol": "R", "intent": "FLIP", "reason": "risk:gross_exposure_exceeded"},
        {"symbol": "C", "intent": "HOLD", "reason": "risk:gross_exposure_exceeded"},
        {"symbol": "D", "intent": "OPEN_LONG", "reason": "risk:risk_per_trade_exceeded"},
    ]

    assert summarize_gross_rejections(decisions) == {
        "rejected_opens": 3,
        "symbols": ["A", "B", "R"],
    }


def test_summarize_gross_rejections_returns_none_without_rejected_openings() -> None:
    assert (
        summarize_gross_rejections(
            [
                {"symbol": "A", "intent": "REDUCE", "reason": "risk:gross_exposure_exceeded"},
                {"symbol": "B", "intent": "OPEN_LONG", "reason": "hold"},
            ]
        )
        is None
    )
