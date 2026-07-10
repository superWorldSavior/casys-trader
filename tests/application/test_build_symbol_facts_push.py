"""Push des décisions récentes dans les facts par-symbole (issue #4, pivot push)."""
from datetime import datetime, timezone

from trader.application.decide.planner_batch import build_symbol_facts
from trader.market.market_data import Bar

_NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def _base(**over):
    kwargs = dict(
        data_age_by_symbol={"AAPL": 3.0},
        now=_NOW,
        active_watches_by_symbol={},
    )
    kwargs.update(over)
    return kwargs


def test_pousse_recent_decisions_quand_present():
    facts = build_symbol_facts(
        "AAPL",
        **_base(recent_decisions_by_symbol={"AAPL": [{"action": "BUY", "cycle_ts": "t1"}]}),
    )
    assert facts["recent_decisions"] == [{"action": "BUY", "cycle_ts": "t1"}]


def test_absent_si_pas_de_decisions():
    # Symbole sans historique → clé recent_decisions absente (pas de bruit vide).
    facts = build_symbol_facts("AAPL", **_base(recent_decisions_by_symbol={"MSFT": [{"action": "SELL"}]}))
    assert "recent_decisions" not in facts


def test_retrocompat_sans_le_param():
    # L'ancien appel (sans recent_decisions_by_symbol) marche toujours.
    facts = build_symbol_facts("AAPL", **_base())
    assert "recent_decisions" not in facts
    assert facts["data_age_m"] == 3


def test_pousse_structure_exacte_seulement_pour_le_symbole_decide():
    bars = [
        Bar(
            ts=f"2026-07-05T0{index}:00:00+00:00",
            open=100.0 + index,
            high=102.0 + index,
            low=99.0 + index,
            close=101.0 + index,
            volume=100.0 * index,
        )
        for index in range(1, 6)
    ]

    facts = build_symbol_facts(
        "AAPL",
        **_base(
            bars_by_symbol={"AAPL": bars, "MSFT": bars},
            bar_timeframe_by_symbol={"AAPL": "15m", "MSFT": "1d"},
        ),
    )

    structure = facts["structure"]
    assert structure["timeframe"] == "15m"
    assert structure["bar_as_of"] == "2026-07-05T05:00:00+00:00"
    assert structure["bars_available"] == 5
    assert structure["price"] == 106.0
    assert structure["swing_low_24"] == 100.0
    assert structure["swing_high_24"] == 107.0
    assert structure["atr_pct_14"] is not None
    assert structure["relative_volume_20"] == 5.0 / 2.5


def test_pousse_uniquement_les_contextes_recherche_du_symbole_decide():
    facts = build_symbol_facts(
        "AAPL",
        **_base(
            company_context_by_symbol={
                "AAPL": {"brief_ref": {"brief_id": "aapl-1"}},
                "MSFT": {"brief_ref": {"brief_id": "msft-1"}},
            },
            mandate_context_by_symbol={
                "AAPL": {"mandate_ref": {"mandate_id": "mandate-aapl"}},
                "MSFT": {"mandate_ref": {"mandate_id": "mandate-msft"}},
            },
        ),
    )

    assert facts["company_intelligence"]["brief_ref"]["brief_id"] == "aapl-1"
    assert facts["universe_mandate"]["mandate_ref"]["mandate_id"] == "mandate-aapl"
    assert "MSFT" not in str(facts)
