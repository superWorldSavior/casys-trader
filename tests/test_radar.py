from trader.radar import is_eligible


def test_excluded_symbol_not_eligible() -> None:
    assert not is_eligible(
        "CL=F",
        amplitude=0.10,
        hard_exclusions={"CL=F"},
        atr_floor=0.01,
    )


def test_too_calm_not_eligible() -> None:
    assert not is_eligible(
        "SPY",
        amplitude=0.002,
        hard_exclusions=set(),
        atr_floor=0.01,
    )


def test_missing_amplitude_not_eligible() -> None:
    assert not is_eligible(
        "SPY",
        amplitude=None,
        hard_exclusions=set(),
        atr_floor=0.01,
    )


def test_tradable_is_eligible() -> None:
    assert is_eligible(
        "SPY",
        amplitude=0.03,
        hard_exclusions=set(),
        atr_floor=0.01,
    )
