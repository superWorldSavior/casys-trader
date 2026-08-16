from trader.domain.learnings.scoring import citation_utility, is_known_harmful_utility


def test_citation_utility_needs_ten_updates() -> None:
    assert citation_utility(q_value=-0.34, q_updates=2) == "unknown"
    assert citation_utility(q_value=-0.34, q_updates=35) == "hurts"
    assert citation_utility(q_value=0.18, q_updates=30) == "helps"
    assert citation_utility(q_value=0.0, q_updates=20) == "neutral"


def test_known_harmful_uses_raw_q_not_shrunk() -> None:
    assert is_known_harmful_utility(q_value=-0.01, q_updates=10) is True
    assert is_known_harmful_utility(q_value=0.01, q_updates=10) is False
    assert is_known_harmful_utility(q_value=-0.9, q_updates=9) is False
