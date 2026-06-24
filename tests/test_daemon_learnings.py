from datetime import datetime, timezone

from trader import daemon
from trader.codex_client import Decision
from trader.tools.market import Bar, MarketError
from trader.tools.memory import LearningsStore
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


def _bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
    base = 100.0 if symbol == "SPY" else 200.0
    return [
        Bar(ts="2026-06-05T11:30:00+00:00", open=base, high=base + 1, low=base - 1, close=base, volume=1000.0),
        Bar(ts="2026-06-05T11:45:00+00:00", open=base + 1, high=base + 2, low=base, close=base + 2, volume=1000.0),
        Bar(ts="2026-06-05T11:55:00+00:00", open=base + 2, high=base + 3, low=base + 1, close=base + 4, volume=1000.0),
    ]


def test_run_cycle_ecrit_le_learning_emis_par_lagent(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    def decide(**kwargs):
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.5,
            rationale="range",
            intent="HOLD",
            learning="le range SPY tient depuis 3 reveils",
        )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    recent = LearningsStore(state_dir / "learnings.jsonl").recent()
    assert [item["note"] for item in recent] == ["le range SPY tient depuis 3 reveils"]
    assert recent[0]["symbol"] == "SPY"
    # Le learning porte le résultat de la décision (pour juger les bons choix).
    assert recent[0]["reason"] == "hold"
    assert recent[0]["executed"] is False


def test_run_cycle_injecte_lattribution_dans_le_contexte(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    import json as _json

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    # Un round-trip clôturé déjà dans l'historique de performance.
    with (state_dir / "model_performance.jsonl").open("w", encoding="utf-8") as f:
        f.write(_json.dumps({"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
                             "quantity": 10, "price": 100.0, "confidence": 0.9, "intent": "OPEN_LONG"}) + "\n")
        f.write(_json.dumps({"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
                             "quantity": 10, "price": 110.0, "confidence": None, "intent": "PLANNED_EXIT",
                             "exit_reason": "take_profit"}) + "\n")

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert contexts
    attribution = contexts[0]["attribution"]
    assert attribution["n_closed_trades"] == 1
    assert attribution["realized_pnl"] == 100.0
    assert contexts[0]["meta_performance"]["available"] is False


def test_run_cycle_passe_le_filtre_regime_a_lattribution(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    import json as _json

    _write_runtime_config(tmp_path)
    (tmp_path / "config" / "regime.yaml").write_text(
        "attribution_since: 2026-06-10\nexclude_symbols: [CL=F, GC=F]\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    with (state_dir / "model_performance.jsonl").open("w", encoding="utf-8") as f:
        f.write(_json.dumps({"ts": "2026-06-09T10:00:00+00:00", "symbol": "CL=F", "action": "BUY",
                             "quantity": 1, "price": 100.0, "confidence": 0.9, "intent": "OPEN_LONG"}) + "\n")
        f.write(_json.dumps({"ts": "2026-06-09T11:00:00+00:00", "symbol": "CL=F", "action": "SELL",
                             "quantity": 1, "price": 50.0, "confidence": None, "intent": "PLANNED_EXIT",
                             "exit_reason": "hard_stop"}) + "\n")
        f.write(_json.dumps({"ts": "2026-06-10T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
                             "quantity": 1, "price": 100.0, "confidence": 0.7, "intent": "OPEN_LONG"}) + "\n")
        f.write(_json.dumps({"ts": "2026-06-10T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
                             "quantity": 1, "price": 112.0, "confidence": None, "intent": "PLANNED_EXIT",
                             "exit_reason": "take_profit"}) + "\n")

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    attribution = contexts[0]["attribution"]
    assert attribution["n_closed_trades"] == 1
    assert attribution["realized_pnl"] == 12.0
    assert attribution["by_exit_reason"][0]["reason"] == "take_profit"
    assert attribution["regime"] == {
        "since": "2026-06-10",
        "excluded_symbols": ["CL=F", "GC=F"],
        "n_excluded_trades": 1,
        "min_entry_confidence": 0.7,
        "n_excluded_low_confidence": 0,
    }


def test_run_cycle_reinjecte_les_learnings_recents_dans_le_contexte(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    LearningsStore(state_dir / "learnings.jsonl").append(
        symbol="SPY", note="cassure ratee au dernier reveil", now=now
    )

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert contexts
    learnings = contexts[0]["learnings"]
    assert [item["note"] for item in learnings] == ["cassure ratee au dernier reveil"]


def test_run_cycle_injecte_les_learnings_consolides_scope_aware(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    import json as _json

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    LearningsStore(state_dir / "learnings.jsonl").append(
        symbol="SPY", note="brut recent", now=now
    )
    (state_dir / "learnings_consolidated.json").write_text(
        _json.dumps(
            {
                "watermark": now.isoformat(),
                "global": [{"note": "z seul ne suffit pas"}],
                "by_symbol": {"SPY": [{"note": "SPY reste range"}]},
            },
            ensure_ascii=False,
        )
    )

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    learnings = contexts[0]["learnings"]
    assert learnings["global"] == [{"note": "z seul ne suffit pas"}]
    assert learnings["by_symbol"] == {"SPY": [{"note": "SPY reste range"}]}
    # D6 : plus de bruts réinjectés quand un consolidé existe
    assert "raw_recent" not in learnings


def test_run_cycle_declenche_le_consolidateur_en_fin_de_cycle(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    calls: list[tuple] = []
    contexts: list[dict] = []
    attribution_payload = {
        "n_closed_trades": 1,
        "realized_pnl": -7.0,
        "realized_gross_pnl": 1.0,
        "total_commissions": 8.0,
        "win_rate": 0.0,
        "by_exit_reason": [{"reason": "trailing_stop", "n": 1, "total_pnl": -7.0, "total_commission": 8.0}],
        "by_confidence": [{"bucket": "high", "n": 1, "total_pnl": -7.0}],
        "regime": {"since": None, "excluded_symbols": []},
    }
    attribution_calls: list[tuple] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    def compute_attribution(state_dir_arg, *, since=None, exclude_symbols=(), min_entry_confidence=None):
        attribution_calls.append((state_dir_arg, since, exclude_symbols))
        return attribution_payload

    def maybe_consolidate(raw_store, consolidated_store, **kwargs):
        calls.append(
            (
                raw_store.path.name,
                consolidated_store.path.name,
                kwargs["threshold"],
                kwargs["acpx_agent"],
                kwargs["model"],
                kwargs["timeout_s"],
                kwargs["attribution"],
            )
        )
        return {"triggered": False, "new_raw_count": 0}

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon.attribution, "compute_attribution", compute_attribution)
    monkeypatch.setattr(daemon.consolidator, "maybe_consolidate", maybe_consolidate)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        consolidator_acpx_agent="codex",
        consolidator_model="gpt-5.5/high",
        consolidator_timeout_s=240,
    )

    assert contexts[0]["attribution"] is attribution_payload
    assert calls == [
        ("learnings.jsonl", "learnings_consolidated.json", 50, "codex", "gpt-5.5/high", 240, attribution_payload)
    ]
    assert attribution_calls == [(state_dir, None, ())]


def test_run_cycle_injecte_guardrails_et_regime_families(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    import json as _json

    _write_runtime_config(tmp_path)
    (tmp_path / "mandate" / "guardrails.json").write_text(
        _json.dumps([{"note": "Toute ouverture s'accompagne d'un exit_plan avec stop."}]),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    def rising_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        closes = [base, base + 1, base + 2, base + 3]  # 4 barres -> momentum calculable
        return [
            Bar(
                ts=f"2026-06-05T11:{15 * i:02d}:00+00:00",
                open=c, high=c + 1, low=c - 1, close=c, volume=1000.0,
            )
            for i, c in enumerate(closes)
        ]

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(rising_bars)
    patch_batch(decide)

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY", "QQQ"], sched=sched, data_source=data_source
    )

    assert contexts
    # D6 étape 2 : guardrails humains injectés, nommés à part
    learnings = contexts[0]["learnings"]
    assert learnings["guardrails"] == [
        {"note": "Toute ouverture s'accompagne d'un exit_plan avec stop."}
    ]
    # D2 : biais de régime par famille (SPY+QQQ = indices, momentum up)
    regime = contexts[0]["regime_families"]
    assert regime["indices"]["dir"] == "up"
    assert regime["indices"]["n"] == 2
    assert regime["indices"]["frac"] == 1.0


def test_run_cycle_calcule_regime_families_sur_daily_plutot_que_runtime(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        if interval == "1d":
            closes = [base, base + 1.0, base + 2.0, base + 3.0]
            return [
                Bar(
                    ts=f"2026-06-{2 + index:02d}T00:00:00+00:00",
                    open=close,
                    high=close + 1.0,
                    low=close - 1.0,
                    close=close,
                    volume=1000.0,
                )
                for index, close in enumerate(closes)
            ]
        closes = [base, base - 1.0, base - 2.0, base - 3.0]
        return [
            Bar(
                ts=f"2026-06-05T11:{15 * index:02d}:00+00:00",
                open=close,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1000.0,
            )
            for index, close in enumerate(closes)
        ]

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY", "QQQ"],
        sched=sched,
        data_source=data_source,
    )

    regime = contexts[0]["regime_families"]
    assert regime["indices"]["dir"] == "up"
    assert regime["indices"]["n"] == 2


def test_run_cycle_omet_regime_families_si_daily_indisponible_sans_fallback_intraday(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        if interval == "1d":
            raise MarketError("fetch_failed", f"{symbol}: daily unavailable")
        base = 100.0 if symbol == "SPY" else 200.0
        closes = [base, base + 1.0, base + 2.0, base + 3.0]
        return [
            Bar(
                ts=f"2026-06-05T11:{15 * index:02d}:00+00:00",
                open=close,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1000.0,
            )
            for index, close in enumerate(closes)
        ]

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY", "QQQ"],
        sched=sched,
        data_source=data_source,
    )

    assert contexts[0]["regime_families"] == {}
