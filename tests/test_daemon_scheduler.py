from datetime import datetime, timezone

from trader.runtime import daemon
from trader.agent.client import Decision
from trader.market.market_data import Bar
from trader.planning.scheduler import Scheduler


def _write_runtime_config(root) -> None:
    (root / "config").mkdir()
    (root / "mandate").mkdir()
    (root / "config" / "universe.yaml").write_text(
        "starting_cash: 100000\nsymbols:\n  - SPY\n  - QQQ\n"
    )
    (root / "config" / "risk.yaml").write_text(
        "\n".join(
            [
                "max_position_value: 20000",
                "max_gross_exposure: 100000",
                "max_order_value: 10000",
                "min_equity: 50000",
            ]
        )
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def test_run_cycle_planifie_uniquement_les_symboles_traites(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.7,
            rationale="attente",
            next_wake_in_minutes=12.0),
    )

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        default_wake_minutes=30.0,
        min_wake_minutes=5.0,
        max_wake_minutes=120.0,
    )

    assert [d["symbol"] for d in report["decisions"]] == ["SPY"]
    assert report["decisions"][0]["next_wake_in_minutes"] == 12.0
    assert sched.next_wake("SPY") == datetime(2026, 6, 5, 12, 12, tzinfo=timezone.utc)
    assert sched.next_wake("QQQ") == datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc)


def test_run_cycle_utilise_le_timer_global_par_defaut_si_agent_ne_modifie_pas(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        default_wake_minutes=30.0,
        min_wake_minutes=5.0,
        max_wake_minutes=120.0,
    )

    assert report["decisions"][0]["next_wake_in_minutes"] is None
    assert sched.next_wake("SPY") == datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc)
    assert sched.next_wake("QQQ") == datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc)


def test_run_cycle_retire_un_override_symbole_expire_si_agent_ne_le_renouvelle_pas(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_next_wake("2026-06-05T12:30:00+00:00")
    sched.set_symbol_next_wake("SPY", "2026-06-05T12:00:00+00:00")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert sched.next_wake("SPY") == datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc)


def test_select_due_symbols_respecte_le_scheduler_au_demarrage_incremental(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_next_wake("2026-06-05T12:30:00+00:00")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    due = daemon._select_due_symbols(["SPY", "QQQ"], sched=sched, once=False, bootstrap=False, now=now)

    assert due == []


def test_select_due_symbols_peut_forcer_un_bootstrap_explicitement(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_next_wake("2026-06-05T12:30:00+00:00")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    due = daemon._select_due_symbols(["SPY", "QQQ"], sched=sched, once=False, bootstrap=True, now=now)

    assert due == ["SPY", "QQQ"]
    assert daemon._select_due_symbols(["SPY", "QQQ"], sched=sched, once=False, bootstrap=False, now=now) == []


def test_run_cycle_persiste_une_indicator_watch_de_decision(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.7,
            rationale="veille breakout",
            intent="HOLD",
            indicator_watch={
                "ttl_minutes": 90,
                "logic": "all",
                "conditions": [
                    {"indicator": "z_score", "op": ">=", "value": 1.5, "interval": "15m", "window": 32},
                    {"indicator": "return", "op": ">", "value": 0.01, "interval": "1h", "window": 24},
                ],
            }),
    )

    report = daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert report["decisions"][0]["indicator_watch_created"] is True
    watches = sched.active_indicator_watches(now=now)
    assert len(watches) == 1
    assert watches[0]["symbol"] == "SPY"
    assert [condition["interval"] for condition in watches[0]["conditions"]] == ["15m", "1h"]


def test_scan_indicator_watches_reveille_le_symbole_declenche(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    watch = {
        "id": "spy-watch",
        "symbol": "SPY",
        "created_at": "2026-06-05T12:00:00+00:00",
        "expires_at": "2026-06-05T13:00:00+00:00",
        "logic": "all",
        "on_trigger": "WAKE",
        "conditions": [
            {
                "symbol": "SPY",
                "indicator": "return",
                "op": ">",
                "value": 0.05,
                "interval": "15m",
                "lookback": "5d",
                "window": 3,
            }
        ],
    }
    sched.set_symbol_indicator_watch("SPY", watch)

    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts="t1", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts="t2", open=103.0, high=104.0, low=102.0, close=103.0, volume=1000.0),
        Bar(ts="t3", open=110.0, high=111.0, low=109.0, close=110.0, volume=1000.0),
    ])

    triggered = daemon._scan_indicator_watches(["SPY"], sched=sched, now=now, data_source=data_source)

    assert triggered[0]["symbol"] == "SPY"
    assert sched.active_indicator_watches(now=now) == []
    assert sched.next_wake("SPY") == now


def test_expiration_indicator_watch_reveille_immediatement_le_symbole(tmp_path) -> None:
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    sched.set_next_wake("2026-06-05T13:00:00+00:00")
    sched.set_symbol_indicator_watch(
        "SPY",
        {
            "id": "spy-watch",
            "symbol": "SPY",
            "created_at": "2026-06-05T12:00:00+00:00",
            "expires_at": "2026-06-05T12:05:00+00:00",
            "logic": "all",
            "on_trigger": "WAKE",
            "conditions": [
                {
                    "symbol": "SPY",
                    "indicator": "return",
                    "op": ">",
                    "value": 0.05,
                    "interval": "15m",
                    "window": 3,
                }
            ],
        },
    )

    expired = daemon.cycle_scheduling.expire_indicator_watches(sched, now=now)

    assert expired[0]["symbol"] == "SPY"
    assert sched.next_wake("SPY") == now
    assert sched.due_symbols(["SPY"], now=now) == ["SPY"]


def test_scan_indicator_watches_charge_les_pairs_cross_asset(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    watch = {
        "id": "spy-relative-watch",
        "symbol": "SPY",
        "created_at": "2026-06-05T12:00:00+00:00",
        "expires_at": "2026-06-05T13:00:00+00:00",
        "logic": "all",
        "on_trigger": "WAKE",
        "conditions": [
            {
                "symbol": "SPY",
                "indicator": "relative_strength",
                "op": ">",
                "value": 0.04,
                "interval": "1h",
                "lookback": "5d",
                "window": 3,
            }
        ],
    }
    sched.set_symbol_indicator_watch("SPY", watch)
    calls: list[str] = []

    def get_bars(symbol, lookback, interval):
        calls.append(symbol)
        closes = [100.0, 100.0, 110.0] if symbol == "SPY" else [100.0, 100.0, 100.0]
        return [
            Bar(ts=f"t{index}", open=close, high=close + 1.0, low=close - 1.0, close=close, volume=1000.0)
            for index, close in enumerate(closes)
        ]

    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(get_bars)

    triggered = daemon._scan_indicator_watches(["SPY", "QQQ", "DIA"], sched=sched, now=now, data_source=data_source)

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert set(calls) == {"SPY", "QQQ", "DIA"}
    assert sched.active_indicator_watches(now=now) == []
    assert sched.next_wake("SPY") == now


def test_run_cycle_injecte_les_indicator_triggers_dans_le_contexte(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    captured_context: dict = {}

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])

    def decide(**kwargs) -> Decision:
        captured_context.update(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "trigger inspecte")

    patch_batch(decide)

    trigger = {
        "watch_id": "spy-watch",
        "symbol": "SPY",
        "on_trigger": "ORDER",
        "order": {"action": "BUY", "quantity": 10, "intent": "OPEN_LONG"},
        "matched": [{"indicator": "return", "actual": 0.1, "op": ">", "value": 0.05}],
    }
    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        indicator_triggers=[trigger],
    )

    assert captured_context["indicator_triggers"] == [trigger]
    assert report["indicator_triggers"] == [trigger]


def test_run_cycle_injecte_les_wake_reasons_dans_le_contexte(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    captured_context: dict = {}

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])

    def decide(**kwargs) -> Decision:
        captured_context.update(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "raison de reveil inspectee")

    patch_batch(decide)

    wake_reason = {
        "symbol": "SPY",
        "reason": "watch_expired",
        "watch_id": "SPY:old-watch",
        "on_trigger": "WAKE",
        "expires_at": "2026-06-05T11:59:00+00:00",
        "observed_at": now.isoformat(),
    }
    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        wake_reasons=[wake_reason],
    )

    assert captured_context["wake_reasons"] == [wake_reason]
    assert report["wake_reasons"] == [wake_reason]
