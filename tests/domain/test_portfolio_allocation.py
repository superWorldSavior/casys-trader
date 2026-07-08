"""Garde du calcul d'allocation pur (région, notional, agrégation)."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.portfolio.allocation import (
    AllocationRow,
    allocation_rows,
    notional_by,
    region_of_family,
)


@dataclass(frozen=True)
class _Pos:
    symbol: str
    quantity: float
    avg_price: float


def test_region_derivee_du_prefixe_famille() -> None:
    assert region_of_family("eu_financials") == "EU"
    assert region_of_family("us_materials") == "US"
    assert region_of_family("tw_pcb") == "TW"
    assert region_of_family("?inconnu?") == "AUTRE"
    assert region_of_family("") == "AUTRE"


def test_allocation_notional_side_et_region() -> None:
    positions = [
        _Pos("ACA.PA", 40.0, 12.5),      # long EU
        _Pos("HEXA-B.ST", -350.0, 80.0),  # short EU
        _Pos("2330.TW", 10.0, 100.0),     # long TW
    ]
    sym2fam = {"ACA.PA": "eu_financials", "HEXA-B.ST": "eu_tech", "2330.TW": "tw_pcb"}
    rows = allocation_rows(positions, sym2fam)

    by_sym = {r.symbol: r for r in rows}
    assert by_sym["ACA.PA"].notional == 40.0 * 12.5      # |qty| × avg_price
    assert by_sym["ACA.PA"].side == "long"
    assert by_sym["HEXA-B.ST"].notional == 350.0 * 80.0  # short compté en valeur absolue
    assert by_sym["HEXA-B.ST"].side == "short"
    assert by_sym["2330.TW"].region == "TW"


def test_allocation_ignore_les_positions_nulles() -> None:
    rows = allocation_rows([_Pos("X", 0.0, 10.0)], {})
    assert rows == []


def test_symbole_sans_famille_tombe_en_inconnu() -> None:
    (row,) = allocation_rows([_Pos("ZZZ", 1.0, 5.0)], {})
    assert row.family == "?inconnu?"
    assert row.region == "AUTRE"


def test_notional_by_agrege_par_cles() -> None:
    rows = [
        AllocationRow("EU", "eu_financials", "A", "long", 1, 100.0),
        AllocationRow("EU", "eu_financials", "B", "long", 1, 50.0),
        AllocationRow("US", "us_energy", "C", "long", 1, 30.0),
    ]
    by_region = notional_by(rows, "region")
    assert by_region[("EU",)] == 150.0
    assert by_region[("US",)] == 30.0

    by_rf = notional_by(rows, "region", "family")
    assert by_rf[("EU", "eu_financials")] == 150.0
