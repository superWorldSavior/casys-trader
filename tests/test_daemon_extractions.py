from datetime import datetime, timezone
from types import SimpleNamespace

import argparse
import pytest

from trader.runtime import daemon


def test_build_base_context_preserve_payload_and_evaluation_order(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 7, 8, 10, 15, tzinfo=timezone.utc)
    cycle_id = now.isoformat()
    calls: list[str] = []
    fee_estimator = object()
    broker = object()
    rate_for_symbol = object()
    risk_cfg = {"max_order_value": 10_000.0}
    prices = {"SPY": 101.0, "QQQ": 202.0}
    bars_by_symbol = {"SPY": ["spy-bars"], "QQQ": ["qqq-bars"]}

    class Snap:
        equity = 123_456.0

        def as_context(self, *, fee_estimator):
            calls.append("portfolio")
            assert fee_estimator is fee_estimator_ref
            return {"portfolio": "context"}

    class Store:
        def read(self):
            calls.append("consolidated_read")
            return ["consolidated"]

        def recent(self, *, limit):
            calls.append(f"raw_recent:{limit}")
            return ["raw"]

    fee_estimator_ref = fee_estimator
    snap = Snap()
    sched = object()
    gate_limits = SimpleNamespace(max_risk_per_trade_pct=0.03, max_order_value=12_000.0)

    monkeypatch.setattr(
        daemon.market,
        "human_clock",
        lambda received_now: calls.append("human_clock") or f"human:{received_now.isoformat()}",
    )
    monkeypatch.setattr(
        daemon.market,
        "market_clocks",
        lambda received_now, received_symbols: calls.append("market_clocks")
        or {"symbols": list(received_symbols)},
    )

    def risk_capacity_context(**kwargs):
        calls.append("risk_capacity")
        assert kwargs["symbols"] == ["SPY", "QQQ"]
        assert kwargs["prices"] is prices
        assert kwargs["broker"] is broker
        assert kwargs["gross_exposure"] == 321.0
        assert kwargs["limits"] is gate_limits
        assert kwargs["equity"] == snap.equity
        assert kwargs["rate_of"] is rate_for_symbol
        assert kwargs["currency_of"] is daemon.fx.currency_for
        return {"capacity": "context"}

    monkeypatch.setattr(daemon.risk_capacity, "risk_capacity_context", risk_capacity_context)
    monkeypatch.setattr(
        daemon,
        "_global_plans_summary",
        lambda received_sched, received_now: calls.append("plans_summary")
        or [{"sched": received_sched is sched, "now": received_now.isoformat()}],
    )
    monkeypatch.setattr(
        daemon.live_kpis,
        "compute_live_kpis",
        lambda state_dir: calls.append("live_kpis") or {"state_dir": str(state_dir)},
    )
    monkeypatch.setattr(
        daemon.consolidator,
        "load_guardrails",
        lambda path: calls.append("load_guardrails") or {"guardrails_path": str(path)},
    )

    def build_context_learnings(consolidated, *, raw_recent, guardrails):
        calls.append("build_context_learnings")
        assert consolidated == ["consolidated"]
        assert raw_recent == ["raw"]
        assert guardrails == {"guardrails_path": str(tmp_path / "mandate" / "guardrails.json")}
        return {"learnings": "context"}

    monkeypatch.setattr(daemon.consolidator, "build_context_learnings", build_context_learnings)
    monkeypatch.setattr(
        daemon.family_regime,
        "momentum_from_bars",
        lambda bars: calls.append(f"momentum:{bars[0]}") or f"momentum:{bars[0]}",
    )

    def compute_family_bias(momentum_by_symbol, active_families):
        calls.append("compute_family_bias")
        assert momentum_by_symbol == {
            "SPY": "momentum:spy-bars",
            "QQQ": "momentum:qqq-bars",
        }
        assert active_families == ("equity",)
        return {"regime": "context"}

    monkeypatch.setattr(daemon.family_regime, "compute_family_bias", compute_family_bias)

    context = daemon._build_base_context(
        cycle_id=cycle_id,
        now=now,
        symbols=["SPY", "QQQ", "MISSING"],
        snap=snap,
        portfolio_fee_estimator=fee_estimator,
        risk_cfg=risk_cfg,
        prices=prices,
        broker=broker,
        gross=321.0,
        gate_limits=gate_limits,
        rate_for_symbol=rate_for_symbol,
        cockpit={"cockpit": True},
        stale_market_data={"MISSING": {"reason": "stale"}},
        sched=sched,
        state_dir=tmp_path / "state",
        root=tmp_path,
        attribution_payload={"attribution": True},
        meta_performance_payload={"meta": True},
        consolidated_learnings_store=Store(),
        learnings_store=Store(),
        max_learnings_in_context=7,
        daily_bars_by_symbol=bars_by_symbol,
        active_families=("equity",),
        requestable_indicator_ids=("RSI", "MACD"),
    )

    assert context == {
        "now": cycle_id,
        "now_human": f"human:{cycle_id}",
        "market_clocks": {"symbols": ["SPY", "QQQ", "MISSING"]},
        "portfolio": {"portfolio": "context"},
        "risk_limits": risk_cfg,
        "risk_capacity": {"capacity": "context"},
        "semantic": {"requestable_indicator_ids": ("RSI", "MACD")},
        "cockpit": {"cockpit": True},
        "stale_market_data": {"MISSING": {"reason": "stale"}},
        "active_plans_summary": [{"sched": True, "now": cycle_id}],
        "kpis": {"state_dir": str(tmp_path / "state")},
        "attribution": {"attribution": True},
        "meta_performance": {"meta": True},
        "learnings": {"learnings": "context"},
        "regime_families": {"regime": "context"},
    }
    assert calls == [
        "human_clock",
        "market_clocks",
        "portfolio",
        "risk_capacity",
        "plans_summary",
        "live_kpis",
        "consolidated_read",
        "raw_recent:7",
        "load_guardrails",
        "build_context_learnings",
        "momentum:spy-bars",
        "momentum:qqq-bars",
        "compute_family_bias",
    ]


def test_build_arg_parser_preserves_cli_and_env_defaults(monkeypatch) -> None:
    monkeypatch.setenv("CASYS_DECISION_BATCH_SIZE", "8")
    monkeypatch.setenv("CASYS_DECISION_BATCH_PARALLELISM", "6")
    monkeypatch.setenv("CASYS_AGENT_TOOLS_ENABLED", "1")
    monkeypatch.setenv("CASYS_IB_HOST", "10.0.0.5")
    monkeypatch.setenv("CASYS_IB_PORT", "4003")
    monkeypatch.setenv("CASYS_IB_CLIENT_ID", "44")

    parser = daemon._build_arg_parser()

    assert isinstance(parser, argparse.ArgumentParser)
    args = parser.parse_args([])
    assert args.decision_batch_size == 8
    assert args.decision_batch_parallelism == 6
    assert args.agent_tools is True
    assert args.ib_host == "10.0.0.5"
    assert args.ib_port == 4003
    assert args.ib_client_id == 44

    overridden = parser.parse_args([
        "--live",
        "--once",
        "--decision-batch-size",
        "3",
        "--decision-batch-parallelism",
        "2",
        "--no-agent-tools",
        "--commission-model",
        "none",
    ])
    assert overridden.live is True
    assert overridden.once is True
    assert overridden.decision_batch_size == 3
    assert overridden.decision_batch_parallelism == 2
    assert overridden.agent_tools is False
    assert overridden.commission_model == "none"


def test_main_uses_build_arg_parser(monkeypatch) -> None:
    class StopParsing(Exception):
        pass

    class Parser:
        def parse_args(self, argv):
            assert argv == ["--once"]
            raise StopParsing

    monkeypatch.setattr(daemon.llm, "load_dotenv", lambda: None)
    monkeypatch.setattr(daemon, "_build_arg_parser", lambda: Parser(), raising=False)

    with pytest.raises(StopParsing):
        daemon.main(["--once"])


def test_cycle_process_state_uses_independent_mutable_defaults() -> None:
    first = daemon.CycleProcessState()
    second = daemon.CycleProcessState()

    first.last_llm_at[("/state", "SPY")] = "seen"
    first.last_gross_rejections["/state"] = {"symbols": ["SPY"]}

    assert second.last_llm_at == {}
    assert second.last_gross_rejections == {}
