from datetime import datetime, timezone

from trader import daemon
from trader.codex_client import Decision
from trader.tools.market import Bar, MarketError
from trader.tools.scheduler import Scheduler


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
                "max_orders_per_cycle: 5",
                "min_equity: 50000",
            ]
        )
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def test_run_cycle_injecte_un_cockpit_compact_sans_barres(monkeypatch, tmp_path, patch_batch) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(ts="2026-06-05T11:30:00+00:00", open=base, high=base + 1, low=base - 1, close=base, volume=1000.0),
            Bar(ts="2026-06-05T11:45:00+00:00", open=base + 1, high=base + 2, low=base, close=base + 2, volume=1000.0),
            Bar(ts="2026-06-05T11:55:00+00:00", open=base + 2, high=base + 3, low=base + 1, close=base + 4, volume=1000.0),
        ]

    def decide(**kwargs) -> Decision:
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon.market, "get_bars", bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched)

    assert contexts
    semantic = contexts[0]["semantic"]
    assert "requestable_indicator_ids" in semantic
    assert "features" not in semantic
    assert "bars" not in contexts[0]
    cockpit = contexts[0]["cockpit"]
    return_index = cockpit["cols"].index("r")
    spy_row = next(row for row in cockpit["rows"] if row[0] == "SPY")
    assert spy_row[return_index] == 0.04
    assert contexts[0]["risk_limits"]["max_order_value"] == 10000


def test_run_cycle_resout_une_requete_indicateurs_bornee_avant_decision_finale(monkeypatch, tmp_path, patch_batch) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(ts="2026-06-05T11:30:00+00:00", open=base, high=base + 1, low=base - 1, close=base, volume=1000.0),
            Bar(ts="2026-06-05T11:45:00+00:00", open=base + 1, high=base + 2, low=base, close=base + 2, volume=1000.0),
            Bar(ts="2026-06-05T11:55:00+00:00", open=base + 2, high=base + 3, low=base + 1, close=base + 4, volume=1000.0),
        ]

    calls = {"n": 0}

    def decide(**kwargs) -> Decision:
        calls["n"] += 1
        contexts.append(kwargs["context"])
        if calls["n"] == 1:
            return daemon.codex_client.ContextResearchRequest(
                symbol=kwargs["symbol"],
                rationale="je veux confirmer le spread",
                requests=[
                    daemon.codex_client.IndicatorRequest(
                        symbol="QQQ",
                        indicators=["z_score", "spread_zscore", "return", "volatility"],
                        window=48,
                    )
                ],
            )
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon.market, "get_bars", bars)
    patch_batch(decide)

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        max_context_requests_per_symbol=1,
        max_indicators_per_request=2,
    )

    assert len(contexts) == 2
    assert "bars" not in contexts[0]
    assert "research" not in contexts[0]
    assert "prior_rationale" not in contexts[0]
    assert "bars" not in contexts[1]
    assert contexts[1]["research"]["requests"][0]["symbol"] == "QQQ"
    assert list(contexts[1]["research"]["requests"][0]["indicators"]) == ["z_score", "spread_zscore"]
    # Le 2e exec (stateless) reçoit la rationale du 1er pour ne pas re-raisonner à zéro.
    assert contexts[1]["prior_rationale"] == "je veux confirmer le spread"


def test_run_cycle_daily_bars_echouees_ne_bloquent_pas_le_cockpit(monkeypatch, tmp_path, patch_batch) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []
    requests: list[tuple[str, str, str]] = []

    def runtime_bars(symbol: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(
                ts=f"2026-06-05T{4 + index // 4:02d}:{(index % 4) * 15:02d}:00+00:00",
                open=base + index * 0.5 - 0.25,
                high=1000.0,
                low=0.0,
                close=base + index * 0.5,
                volume=1000.0,
            )
            for index in range(32)
        ]

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        requests.append((symbol, lookback, interval))
        if interval == "1d":
            raise MarketError("fetch_failed", f"{symbol}: daily unavailable")
        return runtime_bars(symbol)

    def decide(**kwargs) -> Decision:
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon.market, "get_bars", bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched)

    assert ("SPY", "1y", "1d") in requests
    cockpit = contexts[0]["cockpit"]
    cols = cockpit["cols"]
    spy_row = next(row for row in cockpit["rows"] if row[0] == "SPY")
    assert spy_row[cols.index("htf")] == "trending_up"
    assert spy_row[cols.index("aligned")] is True


def test_run_cycle_daily_bars_stale_sont_ignorees_pour_le_cockpit(monkeypatch, tmp_path, patch_batch) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []
    requests: list[tuple[str, str, str]] = []

    def runtime_bars(symbol: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(
                ts=f"2026-06-05T{4 + index // 4:02d}:{(index % 4) * 15:02d}:00+00:00",
                open=base + index * 0.5 - 0.25,
                high=1000.0,
                low=0.0,
                close=base + index * 0.5,
                volume=1000.0,
            )
            for index in range(32)
        ]

    def stale_daily_bars(symbol: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(ts="2026-06-01T00:00:00+00:00", open=base - 0.5, high=base + 1.0, low=base - 1.0, close=base, volume=1000.0),
            Bar(ts="2026-06-02T00:00:00+00:00", open=base, high=base + 1.5, low=base - 0.5, close=base + 0.5, volume=1000.0),
            Bar(ts="2026-06-03T00:00:00+00:00", open=base + 4.5, high=base + 5.2, low=base + 4.0, close=base + 5.0, volume=1000.0),
        ]

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        requests.append((symbol, lookback, interval))
        if interval == "1d":
            return stale_daily_bars(symbol)
        return runtime_bars(symbol)

    def decide(**kwargs) -> Decision:
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon.market, "get_bars", bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched)

    assert ("SPY", "1y", "1d") in requests
    cockpit = contexts[0]["cockpit"]
    cols = cockpit["cols"]
    spy_row = next(row for row in cockpit["rows"] if row[0] == "SPY")
    assert spy_row[cols.index("htf")] == "trending_up"
    assert spy_row[cols.index("aligned")] is True
