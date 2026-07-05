from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.market import market_data as market
from trader.runtime import data_source_runtime


NOW = datetime(2026, 7, 5, 8, 0, tzinfo=timezone.utc)


class RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []

    def info(self, *args: object) -> None:
        self.infos.append(args)

    def warning(self, *args: object) -> None:
        self.warnings.append(args)


class FakeCompositeDataSource:
    def __init__(self, *, routes: list[dict], sources: dict[str, object]) -> None:
        self.routes = routes
        self._sources = sources
        self.failed_sources: dict[str, object] = {}

    def consume_failed_sources(self) -> dict[str, object]:
        failed = dict(self.failed_sources)
        self.failed_sources.clear()
        return failed


class FakeYFinanceDataSource:
    pass


class FakeIBDataSource:
    def __init__(self, ib: object, *, reconnect_factory) -> None:
        self.ib = ib
        self.reconnect_factory = reconnect_factory


class FakeBackoff:
    def __init__(self, *, retry_after: timedelta) -> None:
        self.retry_after = retry_after
        self.failures: list[datetime] = []
        self.successes = 0
        self.due_result = True

    def record_failure(self, now: datetime) -> None:
        self.failures.append(now)

    def record_success(self) -> None:
        self.successes += 1

    def due(self, now: datetime) -> bool:
        return self.due_result


def _config(*, profile: str = "paper") -> data_source_runtime.DataSourceRuntimeConfig:
    return data_source_runtime.DataSourceRuntimeConfig(
        use_composite=True,
        routes=[{"source": "yfinance"}, {"source": "ib"}],
        profile=profile,
    )


def test_load_data_source_config_absent_skips_parser(tmp_path: Path) -> None:
    def parse_config_fn(*_args: object, **_kwargs: object) -> tuple[list[dict], str]:
        raise AssertionError("parser should not be called")

    config = data_source_runtime.load_data_source_config(
        config_path=tmp_path / "missing.yaml",
        profile_override="paper",
        parse_config_fn=parse_config_fn,
    )

    assert config == data_source_runtime.DataSourceRuntimeConfig(
        use_composite=False,
        routes=[],
        profile="",
    )


def test_load_data_source_config_uses_injected_parser(tmp_path: Path) -> None:
    config_path = tmp_path / "data_sources.yaml"
    config_path.write_text("profiles: {}\n", encoding="utf-8")
    logger = RecordingLogger()
    calls: list[dict] = []

    def parse_config_fn(*args: object, **kwargs: object) -> tuple[list[dict], str]:
        calls.append({"args": args, "kwargs": kwargs})
        return ([{"source": "ib"}], "paper")

    config = data_source_runtime.load_data_source_config(
        config_path=config_path,
        profile_override="paper",
        logger=logger,
        parse_config_fn=parse_config_fn,
    )

    assert config == data_source_runtime.DataSourceRuntimeConfig(
        use_composite=True,
        routes=[{"source": "ib"}],
        profile="paper",
    )
    assert calls == [
        {
            "args": (config_path,),
            "kwargs": {
                "profile_override": "paper",
                "known_source_names": frozenset({"yfinance", "ib"}),
            },
        }
    ]
    assert logger.infos == [("data_source config validée: profil=%s", "paper")]


def test_build_composite_paper_continues_without_ib() -> None:
    logger = RecordingLogger()
    calls: list[dict] = []

    def connect_ib(*args: object, **kwargs: object) -> object:
        calls.append({"args": args, "kwargs": kwargs})
        raise market.MarketError("ib_unavailable", "closed")

    state = data_source_runtime.build_data_source(
        _config(profile="paper"),
        host="127.0.0.1",
        port=4002,
        client_id=17,
        attach_retry_seconds=300.0,
        now=NOW,
        connect_ib_fn=connect_ib,
        logger=logger,
        composite_cls=FakeCompositeDataSource,
        yfinance_cls=FakeYFinanceDataSource,
        ib_data_source_cls=FakeIBDataSource,
        backoff_cls=FakeBackoff,
    )

    assert calls == [{"args": ("127.0.0.1", 4002, 17), "kwargs": {"market_data_type": 3}}]
    assert isinstance(state.data_source, FakeCompositeDataSource)
    assert sorted(state.composite_available) == ["yfinance"]
    assert isinstance(state.composite_available["yfinance"], FakeYFinanceDataSource)
    assert isinstance(state.ib_attach_backoff, FakeBackoff)
    assert state.ib_attach_backoff.failures == [NOW]
    assert logger.warnings[0][0] == "IB indisponible, profil composite sans IB (%s): %s"


def test_build_composite_prod_requires_ib() -> None:
    def connect_ib(*_args: object, **_kwargs: object) -> object:
        raise market.MarketError("ib_unavailable", "prod needs IB")

    with pytest.raises(market.MarketError, match="prod needs IB"):
        data_source_runtime.build_data_source(
            _config(profile="prod"),
            host="127.0.0.1",
            port=4002,
            client_id=17,
            attach_retry_seconds=300.0,
            now=NOW,
            connect_ib_fn=connect_ib,
            composite_cls=FakeCompositeDataSource,
            yfinance_cls=FakeYFinanceDataSource,
            ib_data_source_cls=FakeIBDataSource,
            backoff_cls=FakeBackoff,
        )


def test_build_composite_prod_disconnects_opened_ib_when_source_build_fails() -> None:
    ib_obj = object()
    disconnected: list[object] = []

    def connect_ib(*_args: object, **_kwargs: object) -> object:
        return ib_obj

    class FailingIBDataSource:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("contracts config broken")

    with pytest.raises(market.MarketError, match="IBDataSource init failed"):
        data_source_runtime.build_data_source(
            _config(profile="prod"),
            host="127.0.0.1",
            port=4002,
            client_id=17,
            attach_retry_seconds=300.0,
            now=NOW,
            connect_ib_fn=connect_ib,
            ib_data_source_cls=FailingIBDataSource,
            disconnect_quietly=disconnected.append,
        )

    assert disconnected == [ib_obj]


def test_build_direct_ib_uses_delayed_market_data_type() -> None:
    calls: list[dict] = []

    def connect_ib(*args: object, **kwargs: object) -> str:
        calls.append({"args": args, "kwargs": kwargs})
        return "ib"

    state = data_source_runtime.build_data_source(
        data_source_runtime.DataSourceRuntimeConfig(use_composite=False, routes=[], profile=""),
        host="10.0.0.2",
        port=4003,
        client_id=44,
        attach_retry_seconds=300.0,
        now=NOW,
        connect_ib_fn=connect_ib,
        ib_data_source_cls=FakeIBDataSource,
    )

    assert isinstance(state.data_source, FakeIBDataSource)
    assert state.data_source.ib == "ib"
    assert calls == [{"args": ("10.0.0.2", 4003, 44), "kwargs": {"market_data_type": 3}}]
    assert state.data_source.reconnect_factory() == "ib"
    assert calls[-1] == {"args": ("10.0.0.2", 4003, 44), "kwargs": {"market_data_type": 3}}


def test_maybe_attach_ib_adds_source_when_backoff_due() -> None:
    logger = RecordingLogger()
    backoff = FakeBackoff(retry_after=timedelta(seconds=300))
    current = data_source_runtime.DataSourceState(
        data_source=FakeCompositeDataSource(routes=_config().routes, sources={"yfinance": FakeYFinanceDataSource()}),
        composite_available={"yfinance": FakeYFinanceDataSource()},
        ib_attach_backoff=backoff,
    )
    calls: list[dict] = []

    def connect_ib(*args: object, **kwargs: object) -> str:
        calls.append({"args": args, "kwargs": kwargs})
        return "ib-attached"

    state = data_source_runtime.maybe_attach_ib(
        current,
        _config(profile="paper"),
        host="127.0.0.1",
        port=4002,
        client_id=17,
        now=NOW,
        connect_ib_fn=connect_ib,
        logger=logger,
        composite_cls=FakeCompositeDataSource,
        ib_data_source_cls=FakeIBDataSource,
    )

    assert calls == [
        {
            "args": ("127.0.0.1", 4002, 17),
            "kwargs": {"market_data_type": 3, "attempts": 1, "backoff_seconds": 0.0},
        }
    ]
    assert sorted(state.composite_available) == ["ib", "yfinance"]
    assert isinstance(state.composite_available["ib"], FakeIBDataSource)
    assert backoff.successes == 1
    assert logger.infos == [("ib_attach: IB rattaché en cours de session",)]


def test_detach_failed_ib_removes_ib_and_records_backoff() -> None:
    logger = RecordingLogger()
    ib_source = object()
    yfinance = FakeYFinanceDataSource()
    composite = FakeCompositeDataSource(routes=_config().routes, sources={"yfinance": yfinance, "ib": ib_source})
    failure = market.MarketError("ib_connect_failed", "lost")
    composite.failed_sources = {"ib": failure}
    state = data_source_runtime.DataSourceState(
        data_source=composite,
        composite_available={"yfinance": yfinance, "ib": ib_source},
        ib_attach_backoff=None,
    )
    disconnected: list[object] = []

    updated = data_source_runtime.detach_failed_ib(
        state,
        _config(profile="paper"),
        now=NOW,
        is_connection_market_error=lambda exc: exc.code == "ib_connect_failed",
        disconnect_quietly=disconnected.append,
        attach_retry_seconds=120.0,
        logger=logger,
        composite_cls=FakeCompositeDataSource,
        backoff_cls=FakeBackoff,
    )

    assert disconnected == [ib_source]
    assert sorted(updated.composite_available) == ["yfinance"]
    assert isinstance(updated.data_source, FakeCompositeDataSource)
    assert updated.data_source._sources == {"yfinance": yfinance}
    assert isinstance(updated.ib_attach_backoff, FakeBackoff)
    assert updated.ib_attach_backoff.failures == [NOW]
    assert logger.warnings == [("ib_attach: IB détaché après échec source, profil paper dégradé",)]
