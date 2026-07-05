"""Push des décisions récentes dans les facts par-symbole (issue #4, pivot push)."""
from datetime import datetime, timezone

from trader.application.planner_batch import build_symbol_facts

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
