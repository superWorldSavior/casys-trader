"""D7 étage B — plans armés : le daemon exécute au déclenchement sans re-appel LLM."""

import json
from datetime import datetime, timezone

import pytest

from trader.runtime import daemon
from trader.agent.client import Decision
from trader.domain.trade_plan import TradePlan
from trader.execution.broker import SimBroker
from trader.market.market_data import Bar
from trader.planning.scheduler import Scheduler


def _open_plans(state_dir):
    db_path = state_dir / "casys.db"
    if db_path.exists():
        from trader.state_db.connection import open_state_db
        from trader.state_db.trade_plan_store import SqliteTradePlanStore

        try:
            return SqliteTradePlanStore(open_state_db(db_path)).open_plans()
        except Exception:
            pass
    path = state_dir / "trade_plans.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [TradePlan.model_validate(item) for item in raw.get("plans", [])]


def _broker_positions(state_dir):
    db_path = state_dir / "casys.db"
    if db_path.exists():
        from trader.state_db.broker_store import SqliteBroker
        from trader.state_db.connection import open_state_db

        return SqliteBroker(open_state_db(db_path)).positions()
    return SimBroker(state_dir / "broker.json").positions()


def _bars_at(now_iso: str):
    def factory(symbol, lookback, interval):
        return [
            Bar(ts=now_iso, open=100.0, high=100.5, low=99.5, close=100.0, volume=1000.0)
            for _ in range(4)
        ]

    return factory


def _bars_with_lows(now_iso: str, lows: list[float]):
    def factory(symbol, lookback, interval):
        return [
            Bar(ts=now_iso, open=100.0, high=102.0, low=low, close=100.0, volume=1000.0)
            for low in lows
        ]

    return factory


def _stale_bars(now: datetime):
    def factory(symbol, lookback, interval):
        if interval == "1d":
            # Daily AUSSI périmé (séance ancienne) : sinon un daily du jour serait
            # jugé frais par séance (§13.4) et le symbole deviendrait analysable.
            return [
                Bar(ts="2026-06-01", open=100.0, high=100.5, low=99.5, close=100.0, volume=1000.0)
                for _ in range(4)
            ]
        stale_ts = now.replace(hour=10).isoformat()
        return [
            Bar(ts=stale_ts, open=100.0, high=100.5, low=99.5, close=100.0, volume=1000.0)
            for _ in range(4)
        ]

    return factory


def _armed_trigger(
    *,
    stop_price: float = 95.0,
    intent: str = "OPEN_LONG",
    exit_plan: dict | None = None,
) -> dict:
    action = "BUY" if intent == "OPEN_LONG" else "SELL"
    return {
        "watch_id": "SPY:abc123",
        "symbol": "SPY",
        "on_trigger": "EXECUTE_ORDER",
        "order": {
            "intent": intent,
            "action": action,
            "qty": 10.0,
            "confidence": 0.9,
            "exit_plan": exit_plan or {"hard_stop": {"type": "price", "price": stop_price}},
            "rationale": "scénario breakout",
            "trade_evaluation_id": "tpe_arm",
        },
    }


def _run(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
    trigger,
    write_runtime_config,
    *,
    bars_factory=None,
) -> tuple[dict, list]:
    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    # 2026-06-05 (vendredi) à 14:30 UTC = 10:30 ET → séance US régulière ouverte
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    llm_calls: list[str] = []

    def decide(**kwargs):
        llm_calls.append(kwargs["symbol"])
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(bars_factory or _bars_at(now.isoformat())),
        indicator_triggers=[trigger],
    )
    return report, llm_calls


def test_plan_arme_execute_sans_appel_llm(monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config) -> None:
    # prix 100, stop 95 (LONG) : cohérent -> exécution directe
    report, llm_calls = _run(
        monkeypatch, tmp_path, patch_batch, make_data_source, _armed_trigger(stop_price=95.0), write_runtime_config
    )

    assert llm_calls == []  # zéro appel modèle
    entry = report["decisions"][0]
    assert entry["executed"] is True
    assert entry["reason"] == "ok"
    assert entry["armed_plan_id"] == "SPY:abc123"
    assert entry["llm_provider"] is None
    assert _broker_positions(tmp_path / "state")["SPY"].quantity == 10.0
    plans = _open_plans(tmp_path / "state")
    assert len(plans) == 1
    assert plans[0].hard_stop_price == 95.0


def test_plan_arme_resout_hard_stop_volatilite_au_tir(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    fresh_vol_calls: list[dict] = []

    def fresh_volatility(symbol, *, entry_price, cockpit, tradable_bars_by_symbol):
        fresh_vol_calls.append(
            {
                "symbol": symbol,
                "entry_price": entry_price,
                "has_cockpit": bool(cockpit),
                "has_bars": symbol in tradable_bars_by_symbol,
            }
        )
        return 2.0

    monkeypatch.setattr(
        daemon.reference_volatility_service,
        "reference_volatility_for_symbol",
        fresh_volatility,
    )
    trigger = _armed_trigger(
        exit_plan={"hard_stop": {"type": "volatility_multiple", "multiple": 1.5}},
    )

    report, llm_calls = _run(monkeypatch, tmp_path, patch_batch, make_data_source, trigger, write_runtime_config)

    assert llm_calls == []
    entry = report["decisions"][0]
    assert entry["executed"] is True
    plans = _open_plans(tmp_path / "state")
    assert len(plans) == 1
    assert plans[0].hard_stop_price == pytest.approx(97.0)
    assert plans[0].hard_stop_price != 95.0
    assert fresh_vol_calls == [
        {"symbol": "SPY", "entry_price": 100.0, "has_cockpit": True, "has_bars": True},
    ]
    events = [
        json.loads(line)
        for line in (tmp_path / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    resolved_events = [item for item in events if item.get("event") == "armed_plan_resolved"]
    assert resolved_events
    assert resolved_events[0]["symbol"] == "SPY"
    assert resolved_events[0]["plan_id"] == "SPY:abc123"
    assert resolved_events[0]["trace"]["hard_stop"]["resolved_price"] == pytest.approx(97.0)


def test_plan_arme_resout_hard_stop_structural_sur_barres_fraiches(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    trigger = _armed_trigger(
        exit_plan={
            "hard_stop": {
                "type": "structural",
                "anchor": "swing_low",
                "window": 3,
                "buffer_pct": 0.01,
            }
        },
    )

    report, llm_calls = _run(
        monkeypatch,
        tmp_path,
        patch_batch,
        make_data_source,
        trigger,
        write_runtime_config,
        bars_factory=_bars_with_lows(
            "2026-06-05T14:25:00+00:00",
            [90.0, 96.0, 94.0, 97.0],
        ),
    )

    assert llm_calls == []
    assert report["decisions"][0]["executed"] is True
    plans = _open_plans(tmp_path / "state")
    assert len(plans) == 1
    assert plans[0].hard_stop_price == pytest.approx(93.0)
    events = [
        json.loads(line)
        for line in (tmp_path / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    resolved_events = [item for item in events if item.get("event") == "armed_plan_resolved"]
    assert resolved_events[0]["trace"]["hard_stop"]["level"] == pytest.approx(94.0)
    assert resolved_events[0]["trace"]["hard_stop"]["resolved_price"] == pytest.approx(93.0)


def test_plan_arme_structural_sans_barres_au_tir_est_annule(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    def remove_fresh_bars(symbol, *, entry_price, cockpit, tradable_bars_by_symbol):
        tradable_bars_by_symbol.pop(symbol, None)
        return None

    monkeypatch.setattr(
        daemon.reference_volatility_service,
        "reference_volatility_for_symbol",
        remove_fresh_bars,
    )
    trigger = _armed_trigger(
        exit_plan={"hard_stop": {"type": "structural", "anchor": "swing_low", "window": 3}},
    )

    report, llm_calls = _run(monkeypatch, tmp_path, patch_batch, make_data_source, trigger, write_runtime_config)

    assert llm_calls == ["SPY"]
    assert report["decisions"][0]["executed"] is False
    assert "SPY" not in _broker_positions(tmp_path / "state")
    events = (tmp_path / "state" / "events.jsonl").read_text(encoding="utf-8")
    assert "armed_plan_cancelled:exit_unresolved:hard_stop_bars_unavailable" in events
    # L'événement d'annulation porte l'exit_plan BRUT armé (observabilité :
    # diagnostiquer un rejet d'exit sur le chemin armé sans fouiller ailleurs).
    parsed = [json.loads(line) for line in events.splitlines() if line.strip()]
    cancelled = [
        e
        for e in parsed
        if e.get("event") == "armed_plan_cancelled"
        and e.get("reason") == "armed_plan_cancelled:exit_unresolved:hard_stop_bars_unavailable"
    ]
    assert len(cancelled) == 1
    assert cancelled[0]["exit_plan"] == {
        "hard_stop": {"type": "structural", "anchor": "swing_low", "window": 3}
    }


def test_plan_arme_persiste_take_profit_risk_multiple_resolu(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    trigger = _armed_trigger(
        exit_plan={
            "hard_stop": {"type": "percent", "percent": 0.03},
            "take_profits": [{"type": "risk_multiple", "r": 2.0, "fraction": 0.5}],
        },
    )

    report, llm_calls = _run(monkeypatch, tmp_path, patch_batch, make_data_source, trigger, write_runtime_config)

    assert llm_calls == []
    assert report["decisions"][0]["executed"] is True
    plans = _open_plans(tmp_path / "state")
    assert len(plans) == 1
    assert plans[0].hard_stop_price == pytest.approx(97.0)
    assert len(plans[0].take_profits) == 1
    assert plans[0].take_profits[0].price == pytest.approx(106.0)
    assert plans[0].take_profits[0].fraction == pytest.approx(0.5)


def test_plan_arme_annule_si_volatilite_indisponible_au_tir(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    monkeypatch.setattr(
        daemon.reference_volatility_service,
        "reference_volatility_for_symbol",
        lambda symbol, *, entry_price, cockpit, tradable_bars_by_symbol: None,
    )
    trigger = _armed_trigger(
        exit_plan={"hard_stop": {"type": "volatility_multiple", "multiple": 1.5}},
    )

    report, llm_calls = _run(monkeypatch, tmp_path, patch_batch, make_data_source, trigger, write_runtime_config)

    assert llm_calls == ["SPY"]
    entry = report["decisions"][0]
    assert entry["executed"] is False
    assert "SPY" not in _broker_positions(tmp_path / "state")
    events = (tmp_path / "state" / "events.jsonl").read_text(encoding="utf-8")
    assert "armed_plan_cancelled" in events
    assert "armed_plan_cancelled:exit_unresolved:hard_stop_volatility_unavailable" in events


def test_plan_arme_incoherent_avec_le_stop_reveille_le_planificateur(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    # prix 100, stop 105 (LONG) : le prix a DÉJÀ franchi le stop -> pas d'exécution
    # aveugle ; le scénario est invalidé, donc on réveille le LLM AVEC le contexte
    # d'annulation pour qu'il re-décide (re-armer autrement, ou laisser).
    report, llm_calls = _run(
        monkeypatch, tmp_path, patch_batch, make_data_source, _armed_trigger(stop_price=105.0), write_runtime_config
    )

    assert llm_calls == ["SPY"]  # un appel : le planificateur est informé
    entry = report["decisions"][0]
    assert entry["executed"] is False  # le faux LLM répond HOLD
    assert "SPY" not in _broker_positions(tmp_path / "state")
    # télémétrie : l'annulation est tracée en événement
    events = (tmp_path / "state" / "events.jsonl").read_text(encoding="utf-8")
    assert "armed_plan_cancelled" in events
    assert "stop_incoherent" in events


def test_plan_arme_trop_risque_trace_warning_puis_fusible_notionnel(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    # qty énorme : le budget risque est seulement tracé ; le plafond notionnel refuse.
    trigger = _armed_trigger(stop_price=95.0)
    trigger["order"]["qty"] = 5_000.0  # 5000 × 5 de stop_distance = 25000 >> 1% equity
    report, llm_calls = _run(monkeypatch, tmp_path, patch_batch, make_data_source, trigger, write_runtime_config)

    assert llm_calls == []
    entry = report["decisions"][0]
    assert entry["executed"] is False
    assert entry["reason"] == "risk:order_value_exceeded"
    assert entry["qty"] == 5_000.0
    assert entry.get("risk_clamped") is False
    assert entry["risk_warnings"][0]["code"] == "risk_per_trade_exceeded"
    assert "requested_qty" not in entry


def test_plan_arme_sur_position_existante_reveille_le_planificateur(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    # une position existe déjà : pas d'exécution mécanique d'un plan d'OUVERTURE
    # potentiellement périmé — le planificateur re-décide (review Codex).
    from trader.execution.broker import Order

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 5.0), 100.0, "2026-06-11T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    llm_calls: list[str] = []

    def decide(**kwargs):
        llm_calls.append(kwargs["symbol"])
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(_bars_at(now.isoformat())),
        indicator_triggers=[_armed_trigger(stop_price=95.0)],
    )

    assert llm_calls == ["SPY"]  # réveil planificateur, pas d'exécution aveugle
    assert _broker_positions(state_dir)["SPY"].quantity == 5.0  # inchangée
    events = (state_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "position_exists" in events


def test_plan_arme_trace_sa_provenance_dans_le_ledger(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    import json as _json

    trigger = _armed_trigger(stop_price=95.0)
    _run(monkeypatch, tmp_path, patch_batch, make_data_source, trigger, write_runtime_config)

    rows = [
        _json.loads(line)
        for line in (tmp_path / "state" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    executed = [r for r in rows if r.get("executed")]
    assert executed and executed[0]["source"] == "armed_plan"
    assert executed[0]["runtime"]["armed_plan_order"] == trigger["order"]


def test_plan_arme_declenche_stale_trace_l_ordre_dans_le_ledger_meme_si_backoff(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    import json as _json

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 2)
    trigger = _armed_trigger(stop_price=95.0)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=make_data_source(_stale_bars(now)),
        indicator_triggers=[trigger],
    )

    assert report["decisions"][0]["reason"] == "stale_market_data"
    rows = [
        _json.loads(line)
        for line in (state_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["source"] == "daemon"
    assert rows[0]["decision_source"] == "infra"
    assert rows[0]["model_called"] is False
    assert rows[0]["runtime"]["armed_plan_order"] == trigger["order"]


def test_deux_plans_du_meme_symbole_au_meme_cycle_reveillent_le_planificateur(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    # scénarios alternatifs qui déclenchent ENSEMBLE = ambiguïté : on n'exécute
    # pas arbitrairement le dernier, on réveille le planificateur (D7, Erwan).
    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    llm_calls: list[str] = []

    def decide(**kwargs):
        llm_calls.append(kwargs["symbol"])
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    plan_long = _armed_trigger(stop_price=95.0, intent="OPEN_LONG")
    plan_short = _armed_trigger(stop_price=105.0, intent="OPEN_SHORT")
    plan_short["watch_id"] = "SPY:def456"

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(_bars_at(now.isoformat())),
        indicator_triggers=[plan_long, plan_short],
    )

    assert llm_calls == ["SPY"]  # le planificateur arbitre, pas le hasard
    assert "SPY" not in _broker_positions(state_dir)
    events = (state_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "armed_plan_conflict" in events
