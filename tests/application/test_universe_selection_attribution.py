from __future__ import annotations

from trader.application.universe.selection_attribution import (
    UniverseSelection,
    evaluate_selection,
    evaluate_selections,
    selections_from_mandate_payload,
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


def _selection(**overrides) -> UniverseSelection:
    payload = {
        "mandate_id": "m-1",
        "symbol": "AIR.PA",
        "role": "core_candidate",
        "allowed_sides": ("long",),
        "as_of": "2026-01-01T08:00:00+00:00",
        "venue": "EU",
        "family": "defense_aero_eu",
    }
    payload.update(overrides)
    return UniverseSelection(**payload)


def _path_bars(start: float, end: float) -> list[Bar]:
    bars = [_bar(f"2026-01-{day:02d}T16:00:00+00:00", start) for day in range(1, 6)]
    bars.append(_bar("2026-01-06T16:00:00+00:00", end))
    return bars


def test_selection_long_haussiere_via_datasource_est_gagnante() -> None:
    start = 100.0
    end = start * (1.0 + SIGNIFICANT_RETURN_BAND + 0.002)
    source = _FakeSource({"AIR.PA": _path_bars(start, end)})
    evaluated = evaluate_selections([_selection()], source, horizon_sessions=5)
    assert len(evaluated) == 1
    assert evaluated[0].verdict == "gagnant"
    assert source.calls == [("AIR.PA", "1y", "1d")]


def test_selection_long_baissiere_via_datasource_est_perdante() -> None:
    start = 100.0
    end = start * (1.0 - SIGNIFICANT_RETURN_BAND - 0.002)
    item = evaluate_selection(_selection(), _path_bars(start, end), horizon_sessions=5)
    assert item is not None
    assert item.verdict == "perdant"


def test_selection_non_directionnelle_est_non_evaluable() -> None:
    empty = evaluate_selection(
        _selection(allowed_sides=()),
        _path_bars(100.0, 110.0),
        horizon_sessions=5,
    )
    both = evaluate_selection(
        _selection(allowed_sides=("long", "short")),
        _path_bars(100.0, 110.0),
        horizon_sessions=5,
    )
    assert empty is not None and empty.verdict == "non_evaluable"
    assert both is not None and both.verdict == "non_evaluable"


def test_selections_from_active_slice_and_full_mandate() -> None:
    slice_rows = selections_from_mandate_payload(
        {
            "mandate_ref": {
                "mandate_id": "m-eu",
                "venue": "EU",
                "as_of": "2026-07-11T08:00:00+00:00",
            },
            "symbol_mandate": {
                "symbol": "AIR.PA",
                "role": "core_candidate",
                "allowed_sides": ["long"],
                "family_context": {"family": "defense_aero_eu"},
            },
        }
    )
    full_rows = selections_from_mandate_payload(
        {
            "mandate_id": "m-eu",
            "venue": "EU",
            "as_of": "2026-07-11T08:00:00+00:00",
            "symbols": {
                "AIR.PA": {
                    "symbol": "AIR.PA",
                    "role": "core_candidate",
                    "allowed_sides": ["long"],
                    "family_context": {"family": "defense_aero_eu"},
                }
            },
        }
    )
    assert len(slice_rows) == 1
    assert slice_rows[0].family == "defense_aero_eu"
    assert slice_rows[0].allowed_sides == ("long",)
    assert full_rows == slice_rows


def test_summarize_outcomes_separe_pays_et_decoit() -> None:
    summary = summarize_outcomes(
        [
            {
                "family": "alpha",
                "venue": "EU",
                "role": "core_candidate",
                "verdict": "gagnant",
                "forward_return": 0.02,
                "flair_score": 0.08,
                "horizon_sessions": 5,
            },
            {
                "family": "beta",
                "venue": "US",
                "role": "watch_only",
                "verdict": "perdant",
                "forward_return": -0.03,
                "flair_score": -0.08,
                "horizon_sessions": 5,
            },
        ]
    )
    assert summary["pays"]["families"] == ["alpha"]
    assert summary["decoit"]["families"] == ["beta"]
    assert summary["n_gagnant"] == 1
    assert summary["n_perdant"] == 1
