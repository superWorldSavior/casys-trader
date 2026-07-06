from trader.runtime.daemon import summarize_gross_rejections


def test_summarize_extrait_les_ouvertures_recalees_gross() -> None:
    decisions = [
        {"symbol": "A", "intent": "OPEN_LONG", "reason": "risk:gross_exposure_exceeded"},
        {"symbol": "B", "intent": "HOLD", "reason": "hold"},
        {"symbol": "C", "intent": "OPEN_SHORT", "reason": "risk:gross_exposure_exceeded"},
        {"symbol": "D", "intent": "OPEN_LONG", "reason": "risk:risk_per_trade_exceeded"},
    ]
    assert summarize_gross_rejections(decisions) == {
        "rejected_opens": 2,
        "symbols": ["A", "C"],
    }


def test_summarize_compte_add_comme_augmentation_exposition() -> None:
    decisions = [
        {"symbol": "A", "intent": "SCALE_IN", "reason": "risk:gross_exposure_exceeded"},
        {"symbol": "B", "intent": "OPEN_LONG", "reason": "risk:gross_exposure_exceeded"},
    ]

    assert summarize_gross_rejections(decisions) == {
        "rejected_opens": 2,
        "symbols": ["A", "B"],
    }


def test_summarize_ignore_les_non_ouvertures() -> None:
    # Seules les ouvertures augmentent le gross et peuvent tripper le gate ; un
    # rejet gross sur un intent non-ouvreur (cas théorique) n'est pas compté.
    decisions = [
        {"symbol": "Z", "intent": "REDUCE", "reason": "risk:gross_exposure_exceeded"},
    ]
    assert summarize_gross_rejections(decisions) is None


def test_summarize_none_sans_rejet_gross() -> None:
    assert summarize_gross_rejections([{"symbol": "X", "intent": "HOLD", "reason": "hold"}]) is None
    assert summarize_gross_rejections([]) is None
