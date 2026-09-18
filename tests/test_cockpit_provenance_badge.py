"""Provenance badges for the kpis/attribution fail-open fallbacks."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trader.interfaces.cockpit.derive import provenance_badge
from trader.interfaces.cockpit.pages.home import build_equity_summary
from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

NOW = datetime(2026, 7, 6, 2, 1, 28, tzinfo=UTC)


def _render(renderable, width: int = 200) -> str:
    from rich.console import Console

    console = Console(width=width, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_provenance_badge_is_silent_when_live_or_unknown() -> None:
    assert provenance_badge({"kpis_source": "live"}, "kpis") is None
    assert provenance_badge({}, "kpis") is None
    assert provenance_badge({"attribution_source": "live"}, "attribution") is None
    assert provenance_badge({}, "attribution") is None


def test_provenance_badge_labels_report_and_empty() -> None:
    assert provenance_badge({"kpis_source": "report"}, "kpis") == "kpis from report"
    assert provenance_badge({"kpis_source": "empty"}, "kpis") == "kpis unavailable"
    assert (
        provenance_badge({"attribution_source": "report"}, "attribution")
        == "attribution from report"
    )
    assert (
        provenance_badge({"attribution_source": "empty"}, "attribution")
        == "attribution unavailable"
    )


def test_provenance_badge_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown provenance key"):
        provenance_badge({}, "equity_curve")


def test_equity_summary_shows_kpis_badge_only_on_fallback() -> None:
    base = {
        "portfolio": {"cash": 80000.0, "equity": 100000.0, "holdings": []},
        "starting_cash": 100000.0,
        "equity_curve": [99000.0, 100000.0],
    }

    assert "kpis from report" not in _render(
        build_equity_summary({**base, "kpis_source": "live"})
    )
    assert "kpis from report" not in _render(build_equity_summary(dict(base)))
    assert "kpis from report" in _render(
        build_equity_summary({**base, "kpis_source": "report"})
    )
    assert "kpis unavailable" in _render(
        build_equity_summary({**base, "kpis_source": "empty"})
    )


def test_closed_trades_footer_shows_attribution_badge_only_on_fallback() -> None:
    base = {
        "recent_trips": [
            {
                "symbol": "SPY",
                "side": "LONG",
                "gross_pnl": 50.0,
                "commission": 5.0,
                "pnl": 45.0,
                "commission_quality": {"status": "available"},
                "exit_ts": "2026-07-03T15:00:00+00:00",
                "holding_minutes": 300.0,
            },
        ],
        "attribution": {"n_closed_trades": 1},
    }

    assert "attribution from report" not in _render(
        build_closed_trades({**base, "attribution_source": "live"}, now=NOW)
    )
    assert "attribution from report" in _render(
        build_closed_trades({**base, "attribution_source": "report"}, now=NOW)
    )
    assert "attribution unavailable" in _render(
        build_closed_trades({**base, "attribution_source": "empty"}, now=NOW)
    )
