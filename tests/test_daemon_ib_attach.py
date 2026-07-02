from datetime import datetime, timedelta, timezone

import pytest

from trader.runtime import daemon
from trader.tools import market as market_mod
from trader.tools.market import Bar, Freshness, MarketError


def _write_runtime_config(root) -> None:
    (root / "config").mkdir()
    (root / "mandate").mkdir()
    (root / "config" / "universe.yaml").write_text("starting_cash: 100000\nsymbols:\n  - SPY\n")
    (root / "config" / "risk.yaml").write_text(
        "\n".join(
            [
                "max_position_value: 20000",
                "max_gross_exposure: 100000",
                "max_order_value: 10000",
                "max_orders_per_cycle: 5",
                "min_equity: 50000",
            ]
        )
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def _write_data_sources_config(root, *, profile: str = "paper") -> None:
    (root / "config" / "data_sources.yaml").write_text(
        f"""profile: {profile}
profiles:
  paper:
    routes:
      - symbols: ["*"]
        sources: [ib, yfinance]
  prod:
    routes:
      - symbols: ["*"]
        sources: [ib]
"""
    )


def _empty_report(now: datetime) -> dict:
    return {
        "ts": now.isoformat(),
        "dry_run": True,
        "symbols_due": ["SPY"],
        "planned_exits": [],
        "exit_watch_triggers": [],
        "decisions": [],
        "portfolio": {"equity": 100000.0, "cash": 100000.0},
        "prices": {},
    }


def _bar(now: datetime) -> Bar:
    return Bar(ts=now.isoformat(), open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)


def test_paper_ib_down_at_boot_runs_cycle_without_immediate_reprobe(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    t0 = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)
    connect_calls: list[dict] = []
    cycle_sources: list[list[str]] = []

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        raise MarketError("ib_connect_failed", "IB down test")

    def run_cycle(**kwargs):
        cycle_sources.append(sorted(kwargs["data_source"]._sources))
        return _empty_report(t0)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    daemon.main(["--once"], now_fn=lambda: t0, sleep_fn=lambda _seconds: None)

    assert len(connect_calls) == 1
    assert cycle_sources == [["yfinance"]]


def test_main_transmet_horloge_injectee_a_run_cycle(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    t0 = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)

    class FakeIB:
        def disconnect(self) -> None:
            pass

    def connect_ib(*_args, **_kwargs):
        return FakeIB()

    def run_cycle(**kwargs):
        assert kwargs["now"] == t0
        return _empty_report(t0)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    daemon.main(["--once"], now_fn=lambda: t0, sleep_fn=lambda _seconds: None)


def test_paper_ib_attach_backoff_ne_relance_pas_avant_echeance(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    current = {"now": datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)}
    connect_calls: list[dict] = []
    cycle_sources: list[list[str]] = []

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        raise MarketError("ib_connect_failed", "IB down test")

    def run_cycle(**kwargs):
        cycle_sources.append(sorted(kwargs["data_source"]._sources))
        return _empty_report(current["now"])

    def sleep(_seconds: float) -> None:
        current["now"] += timedelta(seconds=60)
        if len(cycle_sources) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    with pytest.raises(KeyboardInterrupt):
        daemon.main(
            ["--poll", "0.01", "--ib-attach-retry-seconds", "300"],
            now_fn=lambda: current["now"],
            sleep_fn=sleep,
        )

    assert len(connect_calls) == 1
    assert cycle_sources == [["yfinance"], ["yfinance"]]


def test_paper_ib_attach_reprobe_apres_backoff_rattache_ib(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    current = {"now": datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)}
    connect_calls: list[dict] = []
    cycle_sources: list[list[str]] = []

    class FakeIB:
        def disconnect(self) -> None:
            pass

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        if len(connect_calls) == 1:
            raise MarketError("ib_connect_failed", "IB down test")
        return FakeIB()

    def run_cycle(**kwargs):
        cycle_sources.append(sorted(kwargs["data_source"]._sources))
        return _empty_report(current["now"])

    def sleep(_seconds: float) -> None:
        if len(cycle_sources) == 1:
            current["now"] += timedelta(seconds=301)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    with pytest.raises(KeyboardInterrupt):
        daemon.main(
            ["--poll", "0.01", "--ib-attach-retry-seconds", "300"],
            now_fn=lambda: current["now"],
            sleep_fn=sleep,
        )

    assert [sorted(call) for call in connect_calls] == [
        ["market_data_type"],
        ["attempts", "backoff_seconds", "market_data_type"],
    ]
    assert connect_calls[1]["market_data_type"] == 3
    assert connect_calls[1]["attempts"] == 1
    assert connect_calls[1]["backoff_seconds"] == 0.0
    assert cycle_sources == [["yfinance"], ["ib", "yfinance"]]


def test_paper_ib_attach_source_a_un_reconnect_factory_et_market_data_type_3(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    current = {"now": datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)}
    connect_calls: list[dict] = []
    attached_ib_sources: list[object] = []

    class FakeIB:
        def disconnect(self) -> None:
            pass

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        if len(connect_calls) == 1:
            raise MarketError("ib_connect_failed", "IB down test")
        return FakeIB()

    def run_cycle(**kwargs):
        sources = kwargs["data_source"]._sources
        if "ib" in sources:
            attached_ib_sources.append(sources["ib"])
        return _empty_report(current["now"])

    def sleep(_seconds: float) -> None:
        if not attached_ib_sources:
            current["now"] += timedelta(seconds=301)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    with pytest.raises(KeyboardInterrupt):
        daemon.main(
            ["--poll", "0.01", "--ib-attach-retry-seconds", "300"],
            now_fn=lambda: current["now"],
            sleep_fn=sleep,
        )

    assert connect_calls[1]["market_data_type"] == 3
    assert getattr(attached_ib_sources[0], "_reconnect_factory") is not None


def test_paper_ib_deja_present_ne_declenche_pas_reprobe(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    current = {"now": datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)}
    connect_calls: list[dict] = []
    cycle_sources: list[list[str]] = []

    class FakeIB:
        def disconnect(self) -> None:
            pass

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        return FakeIB()

    def run_cycle(**kwargs):
        cycle_sources.append(sorted(kwargs["data_source"]._sources))
        return _empty_report(current["now"])

    def sleep(_seconds: float) -> None:
        current["now"] += timedelta(seconds=301)
        if len(cycle_sources) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    with pytest.raises(KeyboardInterrupt):
        daemon.main(
            ["--poll", "0.01", "--ib-attach-retry-seconds", "300"],
            now_fn=lambda: current["now"],
            sleep_fn=sleep,
        )

    assert len(connect_calls) == 1
    assert cycle_sources == [["ib", "yfinance"], ["ib", "yfinance"]]


def test_prod_ib_down_ne_lance_pas_run_cycle_et_ne_reprobe_pas_lazy(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="prod")
    state_dir = tmp_path / "state"
    current = {"now": datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)}
    connect_calls: list[dict] = []
    cycles_run: list[bool] = []

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        raise MarketError("ib_connect_failed", "prod IB down test")

    def run_cycle(**_kwargs):
        cycles_run.append(True)
        return _empty_report(current["now"])

    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    with pytest.raises(KeyboardInterrupt):
        daemon.main(
            ["--poll", "0.01"],
            now_fn=lambda: current["now"],
            sleep_fn=sleep,
        )

    assert cycles_run == []
    assert len(connect_calls) == 1
    assert connect_calls[0]["market_data_type"] == 1


def test_lazy_attach_deconnecte_ib_si_ibdatasource_echoue_et_continue(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    current = {"now": datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)}
    connect_calls: list[dict] = []
    cycle_sources: list[list[str]] = []
    opened_ib: list[object] = []

    class FakeIB:
        def __init__(self) -> None:
            self.disconnected = False

        def disconnect(self) -> None:
            self.disconnected = True

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        if len(connect_calls) == 1:
            raise MarketError("ib_connect_failed", "IB down at boot")
        ib = FakeIB()
        opened_ib.append(ib)
        return ib

    class RaisingIBDataSource:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("contracts config broken")

    def run_cycle(**kwargs):
        cycle_sources.append(sorted(kwargs["data_source"]._sources))
        return _empty_report(current["now"])

    def sleep(_seconds: float) -> None:
        if len(cycle_sources) == 1:
            current["now"] += timedelta(seconds=301)
            return
        if len(cycle_sources) == 2:
            current["now"] += timedelta(seconds=60)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "IBDataSource", RaisingIBDataSource, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    with pytest.raises(KeyboardInterrupt):
        daemon.main(
            ["--poll", "0.01", "--ib-attach-retry-seconds", "300"],
            now_fn=lambda: current["now"],
            sleep_fn=sleep,
        )

    assert len(connect_calls) == 2
    assert opened_ib[0].disconnected is True
    assert cycle_sources == [["yfinance"], ["yfinance"], ["yfinance"]]


def test_boot_deconnecte_ib_si_ibdatasource_echoue_et_degrade_paper(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    t0 = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)
    cycle_sources: list[list[str]] = []

    class FakeIB:
        def __init__(self) -> None:
            self.disconnected = False

        def disconnect(self) -> None:
            self.disconnected = True

    fake_ib = FakeIB()

    def connect_ib(*_args, **_kwargs):
        return fake_ib

    class RaisingIBDataSource:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("contracts config broken")

    def run_cycle(**kwargs):
        cycle_sources.append(sorted(kwargs["data_source"]._sources))
        return _empty_report(t0)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "IBDataSource", RaisingIBDataSource, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    daemon.main(["--once"], now_fn=lambda: t0, sleep_fn=lambda _seconds: None)

    assert fake_ib.disconnected is True
    assert cycle_sources == [["yfinance"]]


def test_ib_failure_apres_attach_redegrade_puis_reprobe_apres_backoff(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    current = {"now": datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)}
    connect_calls: list[dict] = []
    cycle_sources: list[list[str]] = []

    class FakeIB:
        def disconnect(self) -> None:
            pass

    class FakeYFinanceDataSource:
        def get_bars(self, *_args, **_kwargs):
            return [_bar(current["now"])]

    class FlakyIBDataSource:
        def __init__(self, *_args, **_kwargs) -> None:
            self.calls = 0

        def get_bars(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return [_bar(current["now"])]
            raise MarketError("ib_fetch_failed", "socket closed")

        def disconnect(self) -> None:
            pass

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        if len(connect_calls) == 1:
            raise MarketError("ib_connect_failed", "IB down at boot")
        return FakeIB()

    def run_cycle(**kwargs):
        data_source = kwargs["data_source"]
        cycle_sources.append(sorted(data_source._sources))
        data_source.get_bars("SPY", "5d", "1h")
        return _empty_report(current["now"])

    def sleep(_seconds: float) -> None:
        if len(cycle_sources) == 1:
            current["now"] += timedelta(seconds=301)
            return
        if len(cycle_sources) in {2, 3}:
            current["now"] += timedelta(seconds=60)
            return
        if len(cycle_sources) == 4:
            current["now"] += timedelta(seconds=301)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "IBDataSource", FlakyIBDataSource, raising=False)
    monkeypatch.setattr(daemon, "YFinanceDataSource", FakeYFinanceDataSource, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)
    monkeypatch.setattr(
        market_mod,
        "assess_freshness",
        lambda *_args, **_kwargs: Freshness(True, None, 0.0),
    )

    with pytest.raises(KeyboardInterrupt):
        daemon.main(
            ["--poll", "0.01", "--ib-attach-retry-seconds", "300"],
            now_fn=lambda: current["now"],
            sleep_fn=sleep,
        )

    assert len(connect_calls) == 3
    assert cycle_sources == [
        ["yfinance"],
        ["ib", "yfinance"],
        ["ib", "yfinance"],
        ["yfinance"],
        ["ib", "yfinance"],
    ]


def test_ib_data_error_apres_attach_ne_detache_pas_ib(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    _write_data_sources_config(tmp_path, profile="paper")
    state_dir = tmp_path / "state"
    current = {"now": datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)}
    connect_calls: list[dict] = []
    cycle_sources: list[list[str]] = []

    class FakeIB:
        def disconnect(self) -> None:
            pass

    class FakeYFinanceDataSource:
        def get_bars(self, *_args, **_kwargs):
            return [_bar(current["now"])]

    class DataErrorIBDataSource:
        def __init__(self, *_args, **_kwargs) -> None:
            self.calls = 0

        def get_bars(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return [_bar(current["now"])]
            raise MarketError("ib_no_data", "SPY: aucune barre IB disponible")

        def disconnect(self) -> None:
            pass

    def connect_ib(*_args, **kwargs):
        connect_calls.append(kwargs)
        if len(connect_calls) == 1:
            raise MarketError("ib_connect_failed", "IB down at boot")
        return FakeIB()

    def run_cycle(**kwargs):
        data_source = kwargs["data_source"]
        cycle_sources.append(sorted(data_source._sources))
        data_source.get_bars("SPY", "5d", "1h")
        return _empty_report(current["now"])

    def sleep(_seconds: float) -> None:
        if len(cycle_sources) == 1:
            current["now"] += timedelta(seconds=301)
            return
        if len(cycle_sources) in {2, 3}:
            current["now"] += timedelta(seconds=60)
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "IBDataSource", DataErrorIBDataSource, raising=False)
    monkeypatch.setattr(daemon, "YFinanceDataSource", FakeYFinanceDataSource, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)
    monkeypatch.setattr(
        market_mod,
        "assess_freshness",
        lambda *_args, **_kwargs: Freshness(True, None, 0.0),
    )

    with pytest.raises(KeyboardInterrupt):
        daemon.main(
            ["--poll", "0.01", "--ib-attach-retry-seconds", "300"],
            now_fn=lambda: current["now"],
            sleep_fn=sleep,
        )

    assert len(connect_calls) == 2
    assert cycle_sources == [
        ["yfinance"],
        ["ib", "yfinance"],
        ["ib", "yfinance"],
        ["ib", "yfinance"],
    ]
