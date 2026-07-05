"""Runtime data-source composition for daemon main()."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from trader.market import market_data as market
from trader.market.data_source import (
    CompositeDataSource,
    YFinanceDataSource,
    parse_data_sources_config,
)
from trader.market.ib_source import IBDataSource, connect_ib
from trader.runtime.ib_attach import IBAttachBackoff


class LoggerLike(Protocol):
    def info(self, *args: object) -> None: ...
    def warning(self, *args: object) -> None: ...


class BackoffLike(Protocol):
    def record_failure(self, now: datetime) -> None: ...
    def record_success(self) -> None: ...
    def due(self, now: datetime) -> bool: ...


ConnectIbFn = Callable[..., object]
DisconnectFn = Callable[[object], None]
ConnectionErrorClassifier = Callable[[market.MarketError], bool]
ParseConfigFn = Callable[..., tuple[list[dict], str]]


@dataclass(frozen=True)
class DataSourceRuntimeConfig:
    use_composite: bool
    routes: list[dict]
    profile: str


@dataclass(frozen=True)
class DataSourceState:
    data_source: object | None
    composite_available: dict[str, object]
    ib_attach_backoff: BackoffLike | None


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


def load_data_source_config(
    *,
    config_path: Path,
    profile_override: str | None,
    logger: LoggerLike | None = None,
    parse_config_fn: ParseConfigFn = parse_data_sources_config,
) -> DataSourceRuntimeConfig:
    if not config_path.exists():
        return DataSourceRuntimeConfig(use_composite=False, routes=[], profile="")

    routes, profile = parse_config_fn(
        config_path,
        profile_override=profile_override,
        known_source_names=frozenset({"yfinance", "ib"}),
    )
    (logger or _default_logger()).info("data_source config validée: profil=%s", profile)
    return DataSourceRuntimeConfig(use_composite=True, routes=routes, profile=profile)


def _disconnect_noop(_resource: object) -> None:
    return None


def _make_ib_source(
    *,
    ib_obj: object,
    host: str,
    port: int,
    client_id: int,
    market_data_type: int,
    connect_ib_fn: ConnectIbFn,
    ib_data_source_cls: type,
) -> object:
    return ib_data_source_cls(
        ib_obj,
        reconnect_factory=lambda: connect_ib_fn(
            host,
            port,
            client_id,
            market_data_type=market_data_type,
        ),
    )


def build_data_source(
    config: DataSourceRuntimeConfig,
    *,
    host: str,
    port: int,
    client_id: int,
    attach_retry_seconds: float,
    now: datetime,
    connect_ib_fn: ConnectIbFn = connect_ib,
    logger: LoggerLike | None = None,
    composite_cls: type = CompositeDataSource,
    yfinance_cls: type = YFinanceDataSource,
    ib_data_source_cls: type = IBDataSource,
    backoff_cls: type = IBAttachBackoff,
    disconnect_quietly: DisconnectFn = _disconnect_noop,
) -> DataSourceState:
    log = logger or _default_logger()
    if not config.use_composite:
        ib = connect_ib_fn(host, port, client_id, market_data_type=3)
        return DataSourceState(
            data_source=_make_ib_source(
                ib_obj=ib,
                host=host,
                port=port,
                client_id=client_id,
                market_data_type=3,
                connect_ib_fn=connect_ib_fn,
                ib_data_source_cls=ib_data_source_cls,
            ),
            composite_available={},
            ib_attach_backoff=None,
        )

    ib_source: object | None = None
    ib_required = config.profile == "prod"
    market_data_type = 1 if ib_required else 3
    try:
        ib_obj = connect_ib_fn(host, port, client_id, market_data_type=market_data_type)
        try:
            ib_source = _make_ib_source(
                ib_obj=ib_obj,
                host=host,
                port=port,
                client_id=client_id,
                market_data_type=market_data_type,
                connect_ib_fn=connect_ib_fn,
                ib_data_source_cls=ib_data_source_cls,
            )
        except Exception as exc:  # noqa: BLE001 - IB source construction boundary
            disconnect_quietly(ib_obj)
            if isinstance(exc, market.MarketError):
                raise
            raise market.MarketError(
                "ib_connect_failed",
                f"IBDataSource init failed: {type(exc).__name__}: {exc}",
            ) from exc
    except market.MarketError as exc:
        if ib_required:
            raise
        log.warning("IB indisponible, profil composite sans IB (%s): %s", exc.code, exc.context)

    available: dict[str, object] = {"yfinance": yfinance_cls()}
    backoff: BackoffLike | None = None
    if ib_source is not None:
        available["ib"] = ib_source
    elif not ib_required:
        backoff = backoff_cls(retry_after=timedelta(seconds=attach_retry_seconds))
        backoff.record_failure(now)

    data_source = composite_cls(routes=config.routes, sources=available)
    log.info("data_source=composite profil=%s sources=%s", config.profile, sorted(available))
    return DataSourceState(
        data_source=data_source,
        composite_available=available,
        ib_attach_backoff=backoff,
    )


def maybe_attach_ib(
    state: DataSourceState,
    config: DataSourceRuntimeConfig,
    *,
    host: str,
    port: int,
    client_id: int,
    now: datetime,
    connect_ib_fn: ConnectIbFn = connect_ib,
    logger: LoggerLike | None = None,
    composite_cls: type = CompositeDataSource,
    ib_data_source_cls: type = IBDataSource,
    disconnect_quietly: DisconnectFn = _disconnect_noop,
) -> DataSourceState:
    if (
        not config.use_composite
        or config.profile != "paper"
        or state.data_source is None
        or state.ib_attach_backoff is None
        or "ib" in state.composite_available
        or not state.ib_attach_backoff.due(now)
    ):
        return state

    log = logger or _default_logger()
    try:
        ib_obj = connect_ib_fn(
            host,
            port,
            client_id,
            market_data_type=3,
            attempts=1,
            backoff_seconds=0.0,
        )
    except market.MarketError as exc:
        state.ib_attach_backoff.record_failure(now)
        log.warning("ib_attach: IB toujours indisponible (%s): %s", exc.code, exc.context)
        return state

    try:
        ib_source = _make_ib_source(
            ib_obj=ib_obj,
            host=host,
            port=port,
            client_id=client_id,
            market_data_type=3,
            connect_ib_fn=connect_ib_fn,
            ib_data_source_cls=ib_data_source_cls,
        )
    except Exception as exc:  # noqa: BLE001 - IB source construction boundary
        disconnect_quietly(ib_obj)
        state.ib_attach_backoff.record_failure(now)
        log.warning("ib_attach: construction source IB échouée (%s): %s", type(exc).__name__, exc)
        return state

    available = {**state.composite_available, "ib": ib_source}
    state.ib_attach_backoff.record_success()
    log.info("ib_attach: IB rattaché en cours de session")
    return DataSourceState(
        data_source=composite_cls(routes=config.routes, sources=available),
        composite_available=available,
        ib_attach_backoff=state.ib_attach_backoff,
    )


def detach_failed_ib(
    state: DataSourceState,
    config: DataSourceRuntimeConfig,
    *,
    now: datetime,
    is_connection_market_error: ConnectionErrorClassifier,
    disconnect_quietly: DisconnectFn,
    attach_retry_seconds: float,
    logger: LoggerLike | None = None,
    composite_cls: type = CompositeDataSource,
    backoff_cls: type = IBAttachBackoff,
) -> DataSourceState:
    consume_failed_sources = getattr(state.data_source, "consume_failed_sources", None)
    failed_sources = consume_failed_sources() if callable(consume_failed_sources) else {}
    ib_failure = failed_sources.get("ib") if isinstance(failed_sources, dict) else None
    if (
        not config.use_composite
        or config.profile != "paper"
        or "ib" not in state.composite_available
        or ib_failure is None
        or not isinstance(ib_failure, market.MarketError)
        or not is_connection_market_error(ib_failure)
    ):
        return state

    disconnect_quietly(state.composite_available["ib"])
    available = {
        name: source
        for name, source in state.composite_available.items()
        if name != "ib"
    }
    backoff = state.ib_attach_backoff
    if backoff is None:
        backoff = backoff_cls(retry_after=timedelta(seconds=attach_retry_seconds))
    backoff.record_failure(now)
    (logger or _default_logger()).warning("ib_attach: IB détaché après échec source, profil paper dégradé")
    return DataSourceState(
        data_source=composite_cls(routes=config.routes, sources=available),
        composite_available=available,
        ib_attach_backoff=backoff,
    )
