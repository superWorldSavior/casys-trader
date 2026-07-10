from __future__ import annotations

from pathlib import Path

from trader.runtime import runtime_shutdown


class FakeLogger:
    def __init__(self) -> None:
        self.infos: list[tuple[object, ...]] = []

    def info(self, *args: object) -> None:
        self.infos.append(args)


class FakePool:
    def __init__(self, calls: list[str], name: str, *, raises: bool = False) -> None:
        self._calls = calls
        self._name = name
        self._raises = raises

    def stop(self) -> None:
        self._calls.append(self._name)
        if self._raises:
            raise RuntimeError(f"{self._name} boom")


def test_shutdown_runtime_resources_stops_pools_disconnects_and_releases_pid() -> None:
    calls: list[str] = []
    logger = FakeLogger()
    data_source = object()
    releases: list[tuple[Path, int]] = []

    runtime_shutdown.shutdown_runtime_resources(
        universe_intelligence_runner=FakePool(calls, "universe"),
        news_macro_runner=FakePool(calls, "news_macro"),
        decide_pool=FakePool(calls, "decide"),
        execute_pool=FakePool(calls, "execute"),
        data_source=data_source,
        pid_file=Path("daemon.pid"),
        pid=4242,
        disconnect_quietly=lambda resource: calls.append("disconnect") if resource is data_source else None,
        release_pid_file=lambda *, pid_file, pid: releases.append((pid_file, pid)),
        logger=logger,
    )

    assert calls == ["universe", "news_macro", "decide", "execute", "disconnect"]
    assert releases == [(Path("daemon.pid"), 4242)]
    assert logger.infos == [
        ("[universe_intelligence] runner arrêté",),
        ("[news_macro] runner arrêté",),
        ("[queue_decide] pool arrêté",),
        ("[queue_execute] pool arrêté",),
    ]


def test_shutdown_runtime_resources_best_effort_continues_after_errors() -> None:
    calls: list[str] = []
    releases: list[tuple[Path, int]] = []

    def release_pid_file(*, pid_file, pid):
        releases.append((pid_file, pid))
        raise RuntimeError("release boom")

    runtime_shutdown.shutdown_runtime_resources(
        universe_intelligence_runner=FakePool(calls, "universe", raises=True),
        news_macro_runner=FakePool(calls, "news_macro", raises=True),
        decide_pool=FakePool(calls, "decide", raises=True),
        execute_pool=FakePool(calls, "execute"),
        data_source=object(),
        pid_file=Path("daemon.pid"),
        pid=4242,
        disconnect_quietly=lambda _resource: calls.append("disconnect"),
        release_pid_file=release_pid_file,
        logger=FakeLogger(),
    )

    assert calls == ["universe", "news_macro", "decide", "execute", "disconnect"]
    assert releases == [(Path("daemon.pid"), 4242)]
