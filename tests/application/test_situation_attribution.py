from __future__ import annotations

from trader.application.analyst.situation_attribution import (
    evaluate_note,
    evaluate_notes,
    summarize_outcomes,
)
from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


class _FakeSource:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.calls: list[tuple[str, str, str]] = []

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        self.calls.append((symbol, lookback, interval))
        return list(self.bars_by_symbol.get(symbol, []))


def _path_bars(start: float, end: float) -> list[Bar]:
    bars = [_bar(f"2026-01-{day:02d}T16:00:00+00:00", start) for day in range(1, 6)]
    bars.append(_bar("2026-01-06T16:00:00+00:00", end))
    return bars


def _note(**overrides) -> dict:
    payload = {
        "id": 4,
        "as_of": "2026-01-01T08:00:00+00:00",
        "venue": "EU",
        "section_type": "family",
        "section_name": "eu_industrials",
        "direction": "bullish",
        "horizon": "court terme",
        "symbols": '["AIR.PA"]',
    }
    payload.update(overrides)
    return payload


def test_famille_bullish_haussiere_via_datasource_est_gagnante() -> None:
    start = 100.0
    end = start * (1.0 + SIGNIFICANT_RETURN_BAND + 0.002)
    families = {"eu_industrials": ["AIR.PA", "SIE.DE"]}
    source = _FakeSource({"AIR.PA": _path_bars(start, end), "SIE.DE": _path_bars(start, end)})
    evaluated = evaluate_notes([_note()], source, families=families)
    assert len(evaluated) == 1
    assert evaluated[0].verdict == "gagnant"
    assert evaluated[0].coverage_n == 2
    assert evaluated[0].horizon_sessions == 5
    assert {call[0] for call in source.calls} == {"AIR.PA", "SIE.DE"}
    assert source.calls[0][1:] == ("1y", "1d")


def test_horizon_immatures_sont_sautes() -> None:
    item = evaluate_note(
        _note(horizon="1-10d"),
        {"AIR.PA": _path_bars(100.0, 110.0)},
        families={"eu_industrials": ["AIR.PA"]},
    )
    assert item is None


def test_mixed_et_zone_sont_non_evaluables_sans_fetch() -> None:
    source = _FakeSource({"AIR.PA": _path_bars(100.0, 110.0)})
    mixed = evaluate_notes(
        [_note(direction="mixed", horizon="court terme")],
        source,
        families={"eu_industrials": ["AIR.PA"]},
    )
    zone = evaluate_notes(
        [_note(section_type="zone", section_name="EU", direction="bullish", horizon="court terme")],
        source,
        families={"eu_industrials": ["AIR.PA"]},
    )
    assert mixed[0].verdict == "non_evaluable"
    assert zone[0].verdict == "non_evaluable"
    assert source.calls == []


def test_alerte_sans_symbole_non_evaluable() -> None:
    item = evaluate_note(
        _note(section_type="alert", section_name="alerts", symbols="[]", horizon="short"),
        {},
        families={},
    )
    assert item is not None
    assert item.verdict == "non_evaluable"


def test_summarize_outcomes_separe_pays_et_decoit() -> None:
    summary = summarize_outcomes(
        [
            {
                "family": "eu_industrials",
                "direction": "bullish",
                "section_type": "family",
                "section_name": "eu_industrials",
                "verdict": "gagnant",
                "forward_return": 0.02,
                "outcome_score": 0.08,
                "horizon_sessions": 10,
            },
            {
                "family": "tw_osat",
                "direction": "bearish",
                "section_type": "family",
                "section_name": "tw_osat",
                "verdict": "perdant",
                "forward_return": 0.03,
                "outcome_score": -0.08,
                "horizon_sessions": 5,
            },
        ]
    )
    assert summary["pays"]["families"] == ["eu_industrials"]
    assert summary["decoit"]["families"] == ["tw_osat"]
    assert summary["n_gagnant"] == 1
    assert summary["n_perdant"] == 1
    buckets = {item["horizon_bucket"] for item in summary["by_horizon"]}
    assert buckets == {"semaines", "court"}
