"""Market snapshot construction for the runtime daemon."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Collection, Protocol, runtime_checkable

from trader.domain.market import fx
from trader.domain.market import sessions as market
from trader.domain.market.execution_eligibility import build_execution_eligibility
from trader.domain.market_data import MarketError
from trader.market.protocols import DataSource

log = logging.getLogger("trader.application.market_snapshot")

DEFAULT_DAILY_LOOKBACK = "1y"
DEFAULT_DAILY_INTERVAL = "1d"


class MissingFxRate(ValueError):
    """Taux FX manquant ou invalide pour une devise non-base."""

    def __init__(self, symbol: str, ccy: str) -> None:
        self.symbol = symbol
        self.ccy = ccy
        super().__init__(f"missing fx rate for {symbol} ({ccy})")


@dataclass(frozen=True)
class MarketSnapshot:
    bars_by_symbol: dict[str, list]
    prices: dict[str, float]
    stale_market_data: dict[str, dict]
    data_age_by_symbol: dict[str, float]
    runtime_data_source_by_symbol: dict[str, str | None]
    daily_bars_by_symbol: dict[str, list]
    tradable_prices: dict[str, float]
    tradable_symbols: list[str]
    tradable_bars_by_symbol: dict[str, list]
    fx_rate_by_ccy: dict[str, float]
    execution_eligibility: dict[str, dict]
    exit_bars_by_symbol: dict[str, list]
    exit_intervals_by_symbol: dict[str, str]

    def try_rate_for_symbol(self, symbol: str) -> float | None:
        """USD/base → 1.0. Devise manquante ou taux invalide → None (jamais 1.0)."""
        ccy = fx.currency_for(symbol)
        if ccy == fx.BASE_CCY:
            return 1.0
        rate = self.fx_rate_by_ccy.get(ccy)
        if rate is None or not math.isfinite(rate) or rate <= 0:
            return None
        return rate

    def rate_for_symbol(self, symbol: str) -> float:
        rate = self.try_rate_for_symbol(symbol)
        if rate is None:
            raise MissingFxRate(symbol=symbol, ccy=fx.currency_for(symbol))
        return rate


@runtime_checkable
class RuntimeSourceReporter(Protocol):
    def last_source(self, symbol: str) -> str | None:
        ...


class ExecutionEligibilityBuilder(Protocol):
    def __call__(
        self,
        symbols: list[str],
        *,
        stale_market_data: dict[str, dict],
        prices: dict[str, float],
        daily_bars_by_symbol: dict[str, list],
        data_age_by_symbol: dict[str, float],
        now: datetime,
        runtime_interval: str,
    ) -> dict[str, dict]:
        ...


class ExitBarsFetcher(Protocol):
    def __call__(
        self,
        *,
        plan_store: object | None,
        data_source: DataSource,
        tradable_bars_by_symbol: dict[str, list],
        tradable_prices: dict[str, float],
        now: datetime,
    ) -> tuple[dict[str, list], dict[str, str]]:
        ...


class FxRateProvider(Protocol):
    """Resolve native-currency rates for the symbols present in this cycle.

    The cycle owns when rates are needed; the runtime owns configuration and
    market-source I/O needed to resolve them.
    """

    def __call__(
        self,
        symbols: Collection[str],
        *,
        data_source: DataSource,
    ) -> dict[str, float]:
        ...


def _default_connection_error(_: MarketError) -> bool:
    return False


def _default_execution_eligibility(
    symbols: list[str],
    *,
    stale_market_data: dict[str, dict],
    prices: dict[str, float],
    daily_bars_by_symbol: dict[str, list],
    data_age_by_symbol: dict[str, float],
    now: datetime,
    runtime_interval: str,
) -> dict[str, dict]:
    return build_execution_eligibility(
        symbols,
        stale_market_data=stale_market_data,
        prices=prices,
        daily_bars_by_symbol=daily_bars_by_symbol,
        data_age_by_symbol=data_age_by_symbol,
        now=now,
        runtime_interval=runtime_interval,
    )


def _default_exit_bars(
    *,
    tradable_bars_by_symbol: dict[str, list],
    runtime_interval: str,
    **_: object,
) -> tuple[dict[str, list], dict[str, str]]:
    return dict(tradable_bars_by_symbol), {
        sym: runtime_interval for sym in tradable_bars_by_symbol
    }


def build_market_snapshot(
    *,
    symbols: list[str],
    data_source: DataSource,
    now: datetime,
    max_market_data_age_minutes: float,
    runtime_interval: str,
    runtime_lookback: str,
    fx_rate_provider: FxRateProvider,
    plan_store: object | None,
    scheduler: object | None = None,
    daily_lookback: str = DEFAULT_DAILY_LOOKBACK,
    daily_interval: str = DEFAULT_DAILY_INTERVAL,
    is_connection_market_error: Callable[[MarketError], bool] = _default_connection_error,
    execution_eligibility_builder: ExecutionEligibilityBuilder | None = None,
    exit_bars_fetcher: ExitBarsFetcher | None = None,
) -> MarketSnapshot:
    bars_by_symbol: dict[str, list] = {}
    prices: dict[str, float] = {}
    stale_market_data: dict[str, dict] = {}
    data_age_by_symbol: dict[str, float] = {}
    runtime_data_source_by_symbol: dict[str, str | None] = {}
    freshness_max_age = max(
        max_market_data_age_minutes,
        market.freshness_budget_minutes(runtime_interval),
    )

    log.debug("[market] loading bars symbols=%d interval=%s", len(symbols), runtime_interval)
    for sym in symbols:
        try:
            bars = data_source.get_bars(sym, lookback=runtime_lookback, interval=runtime_interval)
        except MarketError as exc:
            if is_connection_market_error(exc):
                raise
            log.warning("données indisponibles %s: %s", sym, exc.code)
            continue
        if not bars:
            log.warning("données vides %s", sym)
            continue
        bars_by_symbol[sym] = bars
        prices[sym] = bars[-1].close
        runtime_data_source_by_symbol[sym] = (
            data_source.last_source(sym) if isinstance(data_source, RuntimeSourceReporter) else None
        )
        freshness = market.assess_freshness(bars, now=now, max_age_minutes=freshness_max_age)
        if freshness.age_minutes is not None:
            data_age_by_symbol[sym] = freshness.age_minutes
        if not freshness.fresh:
            stale_market_data[sym] = {
                "last_bar_ts": str(bars[-1].ts),
                "stale_reason": freshness.reason,
                "data_age_minutes": (
                    None if freshness.age_minutes is None else round(freshness.age_minutes, 4)
                ),
            }
    log.debug("[market] loaded ok=%d missing=%d", len(prices), max(0, len(symbols) - len(prices)))
    if stale_market_data:
        log.debug("[market] stale symbols=%s", sorted(stale_market_data))

    try:
        fx_rate_by_ccy = fx_rate_provider(
            prices.keys(),
            data_source=data_source,
        )
    except Exception as exc:  # noqa: BLE001 - provider failure must not break the cycle
        log.warning("fx rate provider échoué (%s), dégradation USD fallback", exc)
        fx_rate_by_ccy = {fx.BASE_CCY: 1.0}

    for symbol in symbols:
        ccy = fx.currency_for(symbol)
        if ccy == fx.BASE_CCY or ccy in fx_rate_by_ccy:
            continue
        if symbol not in stale_market_data:
            stale_market_data[symbol] = {"stale_reason": "missing_fx_rate"}
            log.error("fx %s : taux %s manquant — symbole non-tradable", symbol, ccy)

    if scheduler is not None:
        for sym in symbols:
            if sym in prices and sym not in stale_market_data:
                current_streak = scheduler.get_stale_streak(sym)
                if current_streak > 0:
                    scheduler.reset_stale_streak(sym)

    tradable_prices = {
        symbol: price for symbol, price in prices.items() if symbol not in stale_market_data
    }
    tradable_symbols = [symbol for symbol in symbols if symbol not in stale_market_data]
    tradable_bars_by_symbol = {
        symbol: bars
        for symbol, bars in bars_by_symbol.items()
        if symbol not in stale_market_data
    }

    daily_bars_by_symbol: dict[str, list] = {}
    for sym in symbols:
        try:
            daily_bars = data_source.get_bars(
                sym,
                lookback=daily_lookback,
                interval=daily_interval,
            )
        except MarketError as exc:
            if is_connection_market_error(exc):
                raise
            log.warning("daily data unavailable %s: %s", sym, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 - daily cockpit data is optional
            log.warning("daily data failed %s: %s", sym, exc)
            continue
        if not daily_bars:
            continue
        try:
            freshness = market.assess_daily_freshness(daily_bars, now=now, symbol=sym)
        except Exception as exc:  # noqa: BLE001 - daily cockpit data is optional
            log.warning("daily freshness failed %s: %s", sym, exc)
            continue
        if not freshness.fresh:
            log.warning("daily data stale %s: %s", sym, freshness.reason)
            continue
        daily_bars_by_symbol[sym] = daily_bars

    build_eligibility = execution_eligibility_builder or _default_execution_eligibility
    execution_eligibility = build_eligibility(
        symbols,
        stale_market_data=stale_market_data,
        prices=prices,
        daily_bars_by_symbol=daily_bars_by_symbol,
        data_age_by_symbol=data_age_by_symbol,
        now=now,
        runtime_interval=runtime_interval,
    )

    if exit_bars_fetcher is None:
        exit_bars_by_symbol, exit_intervals_by_symbol = _default_exit_bars(
            plan_store=plan_store,
            data_source=data_source,
            tradable_bars_by_symbol=tradable_bars_by_symbol,
            tradable_prices=tradable_prices,
            now=now,
            runtime_interval=runtime_interval,
        )
    else:
        exit_bars_by_symbol, exit_intervals_by_symbol = exit_bars_fetcher(
            plan_store=plan_store,
            data_source=data_source,
            tradable_bars_by_symbol=tradable_bars_by_symbol,
            tradable_prices=tradable_prices,
            now=now,
        )

    return MarketSnapshot(
        bars_by_symbol=bars_by_symbol,
        prices=prices,
        stale_market_data=stale_market_data,
        data_age_by_symbol=data_age_by_symbol,
        runtime_data_source_by_symbol=runtime_data_source_by_symbol,
        daily_bars_by_symbol=daily_bars_by_symbol,
        tradable_prices=tradable_prices,
        tradable_symbols=tradable_symbols,
        tradable_bars_by_symbol=tradable_bars_by_symbol,
        fx_rate_by_ccy=fx_rate_by_ccy,
        execution_eligibility=execution_eligibility,
        exit_bars_by_symbol=exit_bars_by_symbol,
        exit_intervals_by_symbol=exit_intervals_by_symbol,
    )
