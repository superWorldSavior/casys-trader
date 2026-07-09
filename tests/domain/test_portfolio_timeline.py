"""Garde du replay pur d'exposition portefeuille depuis les fills."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.portfolio.timeline import exposure_timeline


@dataclass(frozen=True)
class _Fill:
    symbol: str
    side: str
    quantity: float
    price: float
    ts: str


@dataclass(frozen=True)
class _FxFill:
    symbol: str
    side: str
    quantity: float
    price: float
    ts: str
    fx_rate: float


def test_exposure_timeline_rejoue_les_fills_dans_l_ordre_chronologique() -> None:
    fills = [
        _Fill("MSFT", "BUY", 2.0, 50.0, "2026-01-01T10:02:00+00:00"),
        _Fill("AAPL", "BUY", 1.0, 100.0, "2026-01-01T10:01:00+00:00"),
    ]

    points = exposure_timeline(fills, {"AAPL": "us_tech", "MSFT": "us_tech"})

    assert [p.ts for p in points] == [
        "2026-01-01T10:01:00+00:00",
        "2026-01-01T10:02:00+00:00",
    ]
    assert [p.notional for p in points] == [100.0, 200.0]


def test_exposure_timeline_agrege_par_famille_avec_le_dernier_prix_connu() -> None:
    fills = [
        _Fill("AAPL", "BUY", 2.0, 100.0, "2026-01-01T10:00:00+00:00"),
        _Fill("MSFT", "BUY", 1.0, 50.0, "2026-01-01T10:01:00+00:00"),
        _Fill("AAPL", "BUY", 1.0, 110.0, "2026-01-01T10:02:00+00:00"),
    ]

    points = exposure_timeline(fills, {"AAPL": "us_tech", "MSFT": "us_tech"})

    assert [p.notional for p in points] == [
        200.0,  # AAPL: 2 * 100
        250.0,  # AAPL dernier prix 100 + MSFT dernier prix 50
        380.0,  # AAPL: 3 * 110 + MSFT: 1 * 50
    ]
    assert all(p.family == "us_tech" for p in points)
    assert all(p.region == "US" for p in points)


def test_exposure_timeline_convertit_par_le_dernier_fx_rate_quand_present() -> None:
    fills = [
        _FxFill("2330.TW", "BUY", 1000.0, 500.0, "2026-01-01T10:00:00+00:00", 0.031),
        _FxFill("2330.TW", "BUY", 1000.0, 550.0, "2026-01-01T10:01:00+00:00", 0.032),
    ]

    points = exposure_timeline(fills, {"2330.TW": "tw_pcb"})

    assert [p.notional for p in points] == [
        15500.0,
        35200.0,
    ]


def test_exposure_timeline_emet_toutes_les_familles_actives_apres_chaque_fill() -> None:
    fills = [
        _Fill("AAPL", "BUY", 2.0, 100.0, "2026-01-01T10:00:00+00:00"),
        _Fill("2330.TW", "BUY", 3.0, 20.0, "2026-01-01T10:01:00+00:00"),
    ]

    points = exposure_timeline(fills, {"AAPL": "us_tech", "2330.TW": "tw_pcb"})

    by_time_family = {(p.ts, p.family): p.notional for p in points}
    assert by_time_family[("2026-01-01T10:01:00+00:00", "us_tech")] == 200.0
    assert by_time_family[("2026-01-01T10:01:00+00:00", "tw_pcb")] == 60.0


def test_exposure_timeline_retire_les_positions_soldees() -> None:
    fills = [
        _Fill("AAPL", "BUY", 2.0, 100.0, "2026-01-01T10:00:00+00:00"),
        _Fill("AAPL", "SELL", 2.0, 105.0, "2026-01-01T10:01:00+00:00"),
        _Fill("MSFT", "BUY", 1.0, 50.0, "2026-01-01T10:02:00+00:00"),
    ]

    points = exposure_timeline(fills, {"AAPL": "us_tech", "MSFT": "us_tech"})

    assert [(p.ts, p.family, p.notional) for p in points] == [
        ("2026-01-01T10:00:00+00:00", "us_tech", 200.0),
        ("2026-01-01T10:02:00+00:00", "us_tech", 50.0),
    ]


def test_exposure_timeline_symbole_sans_famille_tombe_en_autre() -> None:
    (point,) = exposure_timeline(
        [_Fill("ZZZ", "BUY", 1.0, 5.0, "2026-01-01T10:00:00+00:00")],
        {},
    )

    assert point.family == "?inconnu?"
    assert point.region == "AUTRE"
