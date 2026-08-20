"""Pure projections for the cockpit portfolio positions table."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.derive import positions_by_pnl
from trader.support.coercion import dict_list, finite_float


@dataclass(frozen=True)
class PositionRow:
    """Semantic data for one rendered portfolio position."""

    symbol: str
    side: str
    qty: float
    avg: float | None
    last: float | None
    notional: float
    pnl: float
    pnl_basis: str
    pnl_pct: float
    stop_dist: float | None
    stop_left_pct: float | None
    stop_entry_risk_pct: float | None
    is_stale: bool
    data_age_m: float | None


@dataclass(frozen=True)
class PortfolioPositionsProjection:
    """Rows and aggregate figures derived once for a portfolio refresh."""

    rows: list[PositionRow]
    gross_long: float
    gross_short: float
    gross: float
    net_long: float
    unrealized_total: float | None
    unrealized_basis: str
    unrealized_net_coverage: int
    unrealized_positions: int


def pnl_pct(holding: dict) -> float:
    """Return signed unrealized P&L as a percentage of the cost basis."""

    pnl = f.holding_pnl(holding)
    qty = f.holding_quantity(holding)
    avg = finite_float(holding.get("avg_price"), default=None)
    fx_rate = finite_float(holding.get("fx_rate"), default=1.0) or 1.0
    cost = abs(qty * (avg or 0.0) * fx_rate)
    return (pnl / cost * 100.0) if cost > 0 else 0.0


def sort_holdings(holdings: list[dict], sort_mode: int) -> list[dict]:
    """Sort positions by absolute P&L, notional value, or absolute P&L %."""

    if sort_mode == 1:
        return sorted(holdings, key=f.holding_notional, reverse=True)
    if sort_mode == 2:
        return sorted(holdings, key=lambda holding: abs(pnl_pct(holding)), reverse=True)
    return sorted(holdings, key=lambda holding: abs(f.holding_pnl(holding)), reverse=True)


def project_portfolio_positions(
    state: dict,
    sort_mode: int = 0,
) -> PortfolioPositionsProjection:
    """Project positions and their aggregates once for the portfolio page."""

    holdings = sort_holdings(positions_by_pnl(state), sort_mode)
    trade_plans = dict_list(state.get("trade_plans"))
    rows: list[PositionRow] = []
    gross_long = 0.0
    gross_short = 0.0
    unrealized = f.aggregate_holding_pnl(holdings)

    for holding in holdings:
        symbol = f.holding_symbol(holding)
        qty = f.holding_quantity(holding)
        pnl = f.holding_pnl(holding)
        notional = f.holding_notional(holding)
        avg = finite_float(holding.get("avg_price"), default=None)
        last = finite_float(holding.get("last_price"), default=None)
        plan = f.plan_for_symbol(trade_plans, symbol)

        if qty >= 0:
            gross_long += notional
        else:
            gross_short += notional
        rows.append(
            PositionRow(
                symbol=symbol,
                side="L" if qty >= 0 else "S",
                qty=qty,
                avg=avg,
                last=last,
                notional=notional,
                pnl=pnl,
                pnl_basis=(
                    "net" if f.holding_net_pnl(holding) is not None else "gross"
                ),
                pnl_pct=pnl_pct(holding),
                stop_dist=f.stop_distance_pct(plan, last) if plan else None,
                stop_left_pct=f.stop_left_pct(plan, last) if plan else None,
                stop_entry_risk_pct=f.stop_entry_risk_pct(plan) if plan else None,
                is_stale=f.symbol_is_stale(state, symbol),
                data_age_m=f.staleness_age_m(state, symbol),
            )
        )

    gross = gross_long + gross_short
    return PortfolioPositionsProjection(
        rows=rows,
        gross_long=gross_long,
        gross_short=gross_short,
        gross=gross,
        net_long=gross_long - gross_short,
        unrealized_total=unrealized.amount,
        unrealized_basis=unrealized.basis,
        unrealized_net_coverage=unrealized.net_coverage,
        unrealized_positions=unrealized.positions,
    )


def build_positions_rows(state: dict, sort_mode: int = 0) -> list[dict]:
    """Return the historical dictionary representation of projected rows."""

    return [asdict(row) for row in project_portfolio_positions(state, sort_mode).rows]


__all__ = [
    "PortfolioPositionsProjection",
    "PositionRow",
    "build_positions_rows",
    "pnl_pct",
    "project_portfolio_positions",
    "sort_holdings",
]
