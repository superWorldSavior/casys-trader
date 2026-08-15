from __future__ import annotations

from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar
from trader.domain.universe.selection_attribution import (
    DEFAULT_FORWARD_SESSIONS,
    classify_selection_quality,
    directional_action,
    forward_return_over_sessions,
    score_selection_outcomes,
)


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


def _session_bars(start_close: float, end_close: float, *, sessions: int = DEFAULT_FORWARD_SESSIONS) -> list[Bar]:
    bars = [_bar(f"2026-01-{day:02d}T16:00:00+00:00", start_close) for day in range(1, sessions + 1)]
    bars.append(_bar(f"2026-01-{sessions + 1:02d}T16:00:00+00:00", end_close))
    return bars


def test_long_qui_monte_au_dela_de_la_bande_est_gagnant() -> None:
    start = 100.0
    end = start * (1.0 + SIGNIFICANT_RETURN_BAND + 0.001)
    bars = _session_bars(start, end)
    forward = forward_return_over_sessions(bars, "2026-01-01T08:00:00+00:00", DEFAULT_FORWARD_SESSIONS)
    assert forward is not None and forward > SIGNIFICANT_RETURN_BAND
    assert classify_selection_quality(["long"], forward) == "gagnant"


def test_long_qui_baisse_est_perdant() -> None:
    start = 100.0
    end = start * (1.0 - SIGNIFICANT_RETURN_BAND - 0.001)
    bars = _session_bars(start, end)
    forward = forward_return_over_sessions(bars, "2026-01-01T08:00:00+00:00", DEFAULT_FORWARD_SESSIONS)
    assert classify_selection_quality(["long"], forward) == "perdant"


def test_allowed_sides_vide_ou_bidirectionnel_non_evaluable() -> None:
    assert classify_selection_quality([], 0.02) == "non_evaluable"
    assert classify_selection_quality(("long", "short"), 0.02) == "non_evaluable"
    assert classify_selection_quality(["short", "long"], -0.03) == "non_evaluable"
    assert directional_action(["long"]) == "BUY"
    assert directional_action(["short"]) == "SELL"
    assert directional_action(["long", "short"]) is None


def test_flair_lift_famille_gagnante_superieur_a_famille_perdante() -> None:
    rows = [
        *({"id": index, "symbol": f"WIN{index}", "family": "alpha", "verdict": "gagnant"} for index in range(1, 4)),
        *({"id": index, "symbol": f"LOSS{index}", "family": "beta", "verdict": "perdant"} for index in range(4, 7)),
    ]
    result = score_selection_outcomes(rows, shrinkage_k=5.0)
    alpha = [result["scores"][index] for index in (1, 2, 3)]
    beta = [result["scores"][index] for index in (4, 5, 6)]
    assert sum(alpha) / len(alpha) > sum(beta) / len(beta)
