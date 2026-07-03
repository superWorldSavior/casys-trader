"""Tests des builders purs de la nouvelle home (home.py)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trader.ui.palette import PALETTE_LIGHT

UTC = timezone.utc
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


@dataclass
class FakeVital:
    status: str = "alive"
    battement_old: bool = False
    since_seconds: float | None = 2.0


STATE = {
    "portfolio": {"cash": 62589.0, "equity": 99452.0, "holdings": []},
    "starting_cash": 100000.0,
    "dry_run": True,
    "daemon_status": {"model_calls_used": 2, "max_model_calls_per_cycle": 25,
                      "decisions_done": 3, "symbols_total": 10},
    "equity_curve": [100000.0, 99500.0, 99452.0],
    "sessions": {"EU": {"open": "07:00", "close": "15:30"}},
}


def test_status_line_complet_a_largeur_confortable():
    from trader.cockpit.home import build_status_line

    line = build_status_line(STATE, kill_active=False, palette=PALETTE_LIGHT,
                             width=300, now=NOW, vital=FakeVital())
    plain = line.plain
    for fragment in ("VIVANT", "99,452", "DRY", "kill", "LLM 2/25", "cycle 3/10", "EU"):
        assert fragment in plain, fragment


def test_status_line_etroit_garde_les_criticites():
    """À 80 colonnes : vital/équité/mode/kill présents, sparkline droppée."""
    from trader.cockpit.home import build_status_line

    line = build_status_line(STATE, kill_active=True, palette=PALETTE_LIGHT,
                             width=80, now=NOW, vital=FakeVital())
    plain = line.plain
    assert "VIVANT" in plain and "99,452" in plain
    assert "DRY" in plain and "KILL" in plain
    assert "▁" not in plain and "█" not in plain  # sparkline droppée
    assert line.cell_len <= 80


def test_status_line_cycle_absent_hors_batch():
    from trader.cockpit.home import build_status_line

    state = {**STATE, "daemon_status": {"model_calls_used": 0,
                                        "max_model_calls_per_cycle": 25,
                                        "decisions_done": 0, "symbols_total": 0}}
    line = build_status_line(state, kill_active=False, palette=PALETTE_LIGHT,
                             width=300, now=NOW, vital=FakeVital())
    assert "cycle 0/0" not in line.plain
