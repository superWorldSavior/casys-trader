"""Tests du scoring de qualité des décisions sans LLM ni P&L."""

from datetime import timedelta

import pytest

from trader.tools.market import Bar

from backtest.data import HistoryStore
from backtest.decision_quality import aggregate, classify, forward_return, score_decisions


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


@pytest.mark.parametrize(
    ("action", "rendement", "verdict"),
    [
        ("BUY", 0.006, "gagnant"),
        ("BUY", -0.006, "perdant"),
        ("BUY", 0.004, "neutre"),
        ("BUY", None, "non_evaluable"),
        ("SELL", -0.006, "gagnant"),
        ("SELL", 0.006, "perdant"),
        ("SELL", -0.004, "neutre"),
        ("SELL", None, "non_evaluable"),
        ("HOLD", 0.006, "opportunite_manquee"),
        ("HOLD", -0.006, "bonne_prudence"),
        ("HOLD", 0.004, "justifie"),
        ("HOLD", None, "non_evaluable"),
    ],
)
def test_classify_qualifie_les_decisions_selon_la_bande(action: str, rendement: float | None, verdict: str) -> None:
    assert classify(action, rendement, band=0.005) == verdict


def test_forward_return_utilise_le_prix_asof_et_le_prix_posterieur() -> None:
    store = HistoryStore.from_bars(
        {
            "AAPL": [
                _bar("2026-01-01T10:00:00", 100.0),
                _bar("2026-01-01T14:00:00", 104.0),
            ]
        }
    )

    assert forward_return(store, "AAPL", "2026-01-01T10:30:00", timedelta(hours=4)) == pytest.approx(0.04)


def test_forward_return_non_evaluable_si_aucune_barre_nouvelle_dans_horizon() -> None:
    store = HistoryStore.from_bars(
        {
            "AAPL": [
                _bar("2026-01-02T16:00:00", 100.0),
                _bar("2026-01-05T10:00:00", 110.0),
            ]
        }
    )

    assert store.price_after("AAPL", "2026-01-02T16:00:00", timedelta(days=1)) is None
    assert forward_return(store, "AAPL", "2026-01-02T16:00:00", timedelta(days=1)) is None


def test_score_decisions_et_aggregate_comptent_buckets_taux_et_non_evaluables() -> None:
    decisions = [
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "UP", "action": "HOLD", "reason": "hold"},
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "FLAT", "action": "HOLD", "reason": "hold"},
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "BUYWIN", "action": "BUY", "reason": "ok"},
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "MISSING", "action": "SELL", "reason": "ok"},
    ]
    store = HistoryStore.from_bars(
        {
            "UP": [_bar("2026-01-01T10:00:00", 100.0), _bar("2026-01-01T14:00:00", 101.0)],
            "FLAT": [_bar("2026-01-01T10:00:00", 100.0), _bar("2026-01-01T14:00:00", 100.2)],
            "BUYWIN": [_bar("2026-01-01T10:00:00", 100.0), _bar("2026-01-01T14:00:00", 101.0)],
        }
    )

    rows = score_decisions(decisions, store, band=0.005)
    stats = {(row["action"], row["horizon"]): row for row in aggregate(rows)}

    hold_4h = stats[("HOLD", "4h")]
    assert hold_4h["n_total"] == 2
    assert hold_4h["n_evaluable"] == 2
    assert hold_4h["n_non_evaluable"] == 0
    assert hold_4h["buckets"] == {"opportunite_manquee": 1, "justifie": 1}
    assert hold_4h["frileux_rate"] == pytest.approx(0.5)
    assert hold_4h["hit_rate"] is None

    buy_4h = stats[("BUY", "4h")]
    assert buy_4h["buckets"] == {"gagnant": 1}
    assert buy_4h["hit_rate"] == pytest.approx(1.0)
    assert buy_4h["frileux_rate"] is None

    sell_4h = stats[("SELL", "4h")]
    assert sell_4h["n_evaluable"] == 0
    assert sell_4h["n_non_evaluable"] == 1
    assert sell_4h["mean_forward_return"] is None
    assert sell_4h["buckets"] == {"non_evaluable": 1}
    assert sell_4h["hit_rate"] is None
