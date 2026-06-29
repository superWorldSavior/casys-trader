import json
from datetime import datetime, timezone
from itertools import product

import pytest

from trader import daemon
from trader.tools.market import MarketError
from trader.tools.scheduler import Scheduler


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


def test_main_ouvre_une_connexion_ib_par_cycle_et_la_ferme_en_finally(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    events: list[tuple] = []

    class FakeIB:
        def disconnect(self) -> None:
            events.append(("disconnect", self))

    class FakeIBDataSource:
        def __init__(self, ib, *, reconnect_factory=None):
            self.ib = ib
            self.reconnect_factory = reconnect_factory
            events.append(("data_source", ib, reconnect_factory is not None))

        def disconnect(self) -> None:
            self.ib.disconnect()

    ib = FakeIB()

    def connect_ib(host: str, port: int, client_id: int, **_kw):
        events.append(("connect", host, port, client_id))
        return ib

    def run_cycle(**kwargs):
        events.append(("run_cycle", kwargs["data_source"].ib, kwargs["decision_timeout_s"]))
        assert kwargs["data_source"].reconnect_factory is not None
        assert kwargs["sched"].next_wake("SPY") is None
        return _empty_report(now)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "IBDataSource", FakeIBDataSource, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    daemon.main(["--once", "--ib-host", "10.0.0.2", "--ib-port", "4003", "--ib-client-id", "44"])

    assert events == [
        ("connect", "10.0.0.2", 4003, 44),
        ("data_source", ib, True),
        ("run_cycle", ib, 900),
        ("disconnect", ib),
    ]
    assert json.loads((state_dir / "last_report.json").read_text())["symbols_due"] == ["SPY"]


def test_main_garde_connexion_ib_ouverte_entre_les_pauses(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    events: list[tuple] = []

    class FakeIB:
        def disconnect(self) -> None:
            events.append(("disconnect", self))

    class FakeIBDataSource:
        def __init__(self, ib, *, reconnect_factory=None):
            self.ib = ib
            self.reconnect_factory = reconnect_factory
            events.append(("data_source", ib, reconnect_factory is not None))

        def disconnect(self) -> None:
            self.ib.disconnect()

    ib = FakeIB()
    sleeps = 0

    def connect_ib(host: str, port: int, client_id: int, **_kw):
        events.append(("connect", host, port, client_id))
        return ib

    def run_cycle(**kwargs):
        events.append(("run_cycle", kwargs["data_source"].ib))
        return _empty_report(now)

    def sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        events.append(("sleep", seconds))
        if sleeps == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "IBDataSource", FakeIBDataSource, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)
    monkeypatch.setattr(daemon.time, "sleep", sleep)

    with pytest.raises(KeyboardInterrupt):
        daemon.main(["--ib-host", "10.0.0.2", "--ib-port", "4003", "--ib-client-id", "44", "--poll", "0.01"])

    assert events == [
        ("connect", "10.0.0.2", 4003, 44),
        ("data_source", ib, True),
        ("run_cycle", ib),
        ("sleep", 0.0),
        ("run_cycle", ib),
        ("sleep", 0.0),
        ("disconnect", ib),
    ]


def test_main_transmet_le_plafond_decisionnel_cli(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    captured: dict[str, int] = {}

    class FakeIB:
        def disconnect(self) -> None:
            pass

    class FakeIBDataSource:
        def __init__(self, ib, *, reconnect_factory=None):
            self.ib = ib
            self.reconnect_factory = reconnect_factory

        def disconnect(self) -> None:
            self.ib.disconnect()

    def connect_ib(host: str, port: int, client_id: int, **_kw):
        return FakeIB()

    def run_cycle(**kwargs):
        captured["decision_timeout_s"] = kwargs["decision_timeout_s"]
        captured["decision_batch_size"] = kwargs["decision_batch_size"]
        captured["decision_batch_parallelism"] = kwargs["decision_batch_parallelism"]
        return _empty_report(now)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "IBDataSource", FakeIBDataSource, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)

    daemon.main([
        "--once",
        "--decision-timeout-s",
        "321",
        "--decision-batch-size",
        "7",
        "--decision-batch-parallelism",
        "2",
    ])

    assert captured["decision_timeout_s"] == 321
    assert captured["decision_batch_size"] == 7
    assert captured["decision_batch_parallelism"] == 2


def test_main_saute_le_cycle_si_connexion_ib_echoue_et_reessaie_au_reveil_suivant(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []

    def connect_ib(*_args, **_kwargs):
        raise MarketError("ib_connect_failed", "127.0.0.1:4002 clientId=17: refus")

    def run_cycle(**_kwargs):
        raise AssertionError("run_cycle ne doit pas être appelé sans connexion IB")

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
    monkeypatch.setattr(daemon, "run_cycle", run_cycle)
    monkeypatch.setattr(daemon.time, "sleep", sleep)

    with pytest.raises(KeyboardInterrupt):
        daemon.main(["--poll", "0.01"])

    assert sleeps == [0.01]


def test_run_cycle_remonte_les_erreurs_de_connexion_ib(monkeypatch, tmp_path) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"

    class BrokenDataSource:
        def get_bars(self, *_args, **_kwargs):
            raise MarketError("ib_connect_failed", "socket dead")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    with pytest.raises(MarketError) as exc:
        daemon.run_cycle(
            dry_run=True,
            symbols_filter=["SPY"],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=BrokenDataSource(),
        )

    assert exc.value.code == "ib_connect_failed"


def test_ib_maps_couvrent_toutes_les_requetes_de_barres_du_daemon() -> None:
    from trader.tools.ib_source import INTERVAL_MAP, LOOKBACK_MAP

    request_context_intervals = ["15m", "30m", "1h", "4h", "1d"]
    request_context_lookbacks = ["5d", "1mo", "3mo", "6mo", "1y"]
    used_pairs = {
        (daemon.DEFAULT_RUNTIME_INTERVAL, daemon.DEFAULT_RUNTIME_LOOKBACK),
        (daemon.COCKPIT_DAILY_INTERVAL, daemon.COCKPIT_DAILY_LOOKBACK),
        ("1h", "5d"),
        *product(request_context_intervals, request_context_lookbacks),
    }

    missing_intervals = sorted({interval for interval, _lookback in used_pairs if interval not in INTERVAL_MAP})
    missing_lookbacks = sorted({lookback for _interval, lookback in used_pairs if lookback not in LOOKBACK_MAP})

    assert missing_intervals == []
    assert missing_lookbacks == []
