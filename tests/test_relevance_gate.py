from trader.planning import relevance_gate


def _needs(**kwargs) -> tuple[bool, str]:
    base = dict(
        agent_requested_wake=False,
        has_trigger=False,
        has_position=False,
        family_regime_strong=False,
        stretched=False,
        aligned=None,
        sig=None,
        hours_since_last_llm=0.5,
        last_wake_reasons=None,
    )
    base.update(kwargs)
    return relevance_gate.symbol_needs_llm(**base)


def test_reveil_demande_par_l_agent_toujours_honore() -> None:
    # D7 (Erwan) : l'agent garde son autonomie de planification — un réveil
    # qu'il a explicitement demandé (next_wake_in_minutes) n'est JAMAIS gate,
    # même si le marché est calme.
    assert _needs(agent_requested_wake=True) == (True, "agent_wake")


def test_evenements_passent_le_gate() -> None:
    assert _needs(has_trigger=True) == (True, "trigger")
    assert _needs(has_position=True, hours_since_last_llm=None) == (True, "position")
    assert _needs(family_regime_strong=True) == (True, "regime")
    assert _needs(sig=["1h:breakout_up"]) == (True, "signal")


def test_position_ouverte_routiniere_est_debouncee_sous_une_heure() -> None:
    assert relevance_gate.POSITION_REVIEW_DEBOUNCE_HOURS == 1.0
    assert _needs(
        has_position=True,
        hours_since_last_llm=0.99,
    ) == (False, "position_debounce")


def test_position_ouverte_est_revue_a_une_heure_ou_si_jamais_vue() -> None:
    assert _needs(
        has_position=True,
        hours_since_last_llm=1.0,
    ) == (True, "position")
    assert _needs(
        has_position=True,
        hours_since_last_llm=None,
    ) == (True, "position")


def test_nouveau_regime_ou_signal_htf_reveille_une_position_recente() -> None:
    assert _needs(
        has_position=True,
        family_regime_strong=True,
        hours_since_last_llm=0.5,
    ) == (True, "regime")
    assert _needs(
        has_position=True,
        sig=["1h:breakout_down"],
        hours_since_last_llm=0.5,
    ) == (True, "signal")


def test_signal_persistant_ne_contourne_pas_le_debounce_position() -> None:
    assert _needs(
        has_position=True,
        sig=["1h:breakout_down"],
        last_wake_reasons=("signal",),
        last_wake_fingerprints={"signal": "signal:1h:breakout_down"},
        hours_since_last_llm=0.5,
    ) == (False, "position_debounce")


def test_inversion_signal_htf_bypasse_le_debounce_position() -> None:
    assert _needs(
        has_position=True,
        sig=["1h:breakout_down"],
        last_wake_reasons=("signal",),
        last_wake_fingerprints={"signal": "signal:1h:breakout_up"},
        hours_since_last_llm=0.5,
    ) == (True, "signal")


def test_disparition_puis_reapparition_du_meme_signal_reveillent_une_fois() -> None:
    reviewed_signal = {
        "signal": "signal:1h:breakout_up",
    }

    # Le signal actif lors de la dernière revue disparaît : c'est une
    # invalidation matérielle, même si la position a été revue récemment.
    assert _needs(
        has_position=True,
        last_wake_reasons=("signal",),
        last_wake_fingerprints=reviewed_signal,
        hours_since_last_llm=0.5,
    ) == (True, "signal")

    # Après cette revue, le daemon persiste l'état courant vide. La disparition
    # ne réveille donc pas en boucle, mais le même signal qui revient est neuf.
    assert _needs(
        has_position=True,
        last_wake_reasons=(),
        last_wake_fingerprints={},
        hours_since_last_llm=0.5,
    ) == (False, "position_debounce")
    assert _needs(
        has_position=True,
        sig=["1h:breakout_up"],
        last_wake_reasons=(),
        last_wake_fingerprints={},
        hours_since_last_llm=0.5,
    ) == (True, "signal")


def test_signal_15m_seul_ne_reveille_pas() -> None:
    assert _needs(sig=["15m:stretched_up"]) == (False, "quiet")
    assert _needs(sig=["15m:stretched_up"], stretched=True) == (False, "quiet")
    assert _needs(sig=["15m:stretched_up"], stretched=True, aligned=False) == (False, "quiet")


def test_signal_htf_prefixes_reveillent() -> None:
    for token in ("1h:breakout_up", "4h:stretched_up", "1d:breakout_down"):
        assert _needs(sig=[token]) == (True, "signal")


def test_stretched_aligne_reveille_en_signal() -> None:
    assert _needs(stretched=True, aligned=True) == (True, "signal")
    assert _needs(sig=["15m:stretched_up"], stretched=True, aligned=True) == (True, "signal")


def test_stretched_sans_alignement_est_quiet() -> None:
    assert _needs(stretched=True) == (False, "quiet")
    assert _needs(stretched=True, aligned=False) == (False, "quiet")
    assert _needs(stretched=True, aligned=None) == (False, "quiet")


def test_polling_par_defaut_sur_symbole_calme_est_gate() -> None:
    # seul le réveil PAR DÉFAUT (l'agent n'a rien demandé) sur un symbole
    # calme et vu récemment est filtré -> pas d'appel LLM
    assert _needs() == (False, "quiet")


def test_revue_periodique_garantie() -> None:
    # le LLM doit revoir chaque symbole au moins toutes les max_quiet_hours
    # (pour poser/ajuster ses watches), et au premier réveil post-restart.
    assert _needs(hours_since_last_llm=4.0) == (True, "periodic_review")
    assert _needs(hours_since_last_llm=None) == (True, "periodic_review")


def test_regime_debounced_sous_deux_heures() -> None:
    assert relevance_gate.SIGNAL_DEBOUNCE_HOURS == 2.0
    assert _needs(
        family_regime_strong=True,
        family_regime_fingerprint="regime:us:up",
        last_wake_reasons=("regime",),
        last_wake_fingerprints={"regime": "regime:us:up"},
        hours_since_last_llm=1.99,
    ) == (False, "quiet")
    assert _needs(
        family_regime_strong=True,
        family_regime_fingerprint="regime:us:up",
        last_wake_reasons=("regime",),
        last_wake_fingerprints={"regime": "regime:us:up"},
        hours_since_last_llm=2.0,
    ) == (True, "regime")


def test_inversion_regime_bypasse_le_debounce_position() -> None:
    assert _needs(
        has_position=True,
        family_regime_strong=True,
        family_regime_fingerprint="regime:us:down",
        last_wake_reasons=("regime",),
        last_wake_fingerprints={"regime": "regime:us:up"},
        hours_since_last_llm=0.5,
    ) == (True, "regime")


def test_disparition_puis_reapparition_du_meme_regime_reveillent_une_fois() -> None:
    reviewed_regime = {
        "regime": "regime:us:up",
    }

    assert _needs(
        has_position=True,
        last_wake_reasons=("regime",),
        last_wake_fingerprints=reviewed_regime,
        hours_since_last_llm=0.5,
    ) == (True, "regime")

    assert _needs(
        has_position=True,
        last_wake_reasons=(),
        last_wake_fingerprints={},
        hours_since_last_llm=0.5,
    ) == (False, "position_debounce")
    assert _needs(
        has_position=True,
        family_regime_strong=True,
        family_regime_fingerprint="regime:us:up",
        last_wake_reasons=(),
        last_wake_fingerprints={},
        hours_since_last_llm=0.5,
    ) == (True, "regime")


def test_signal_htf_debounced_sous_deux_heures() -> None:
    assert _needs(
        sig=["1h:breakout_up"],
        last_wake_reasons=("signal",),
        last_wake_fingerprints={"signal": "signal:1h:breakout_up"},
        hours_since_last_llm=1.0,
    ) == (False, "quiet")
    assert _needs(
        stretched=True,
        aligned=True,
        last_wake_reasons=("signal",),
        last_wake_fingerprints={"signal": "signal:stretched-aligned"},
        hours_since_last_llm=1.5,
    ) == (False, "quiet")
    assert _needs(
        sig=["4h:breakout_up"],
        last_wake_reasons=("signal",),
        last_wake_fingerprints={"signal": "signal:4h:breakout_up"},
        hours_since_last_llm=2.0,
    ) == (True, "signal")


def test_signal_apres_reveil_regime_n_est_pas_debounced() -> None:
    # punch-through seulement si le signal n'était pas déjà actif à la revue
    assert _needs(
        sig=["1h:breakout_up"],
        last_wake_reasons=("regime",),
        hours_since_last_llm=1.0,
    ) == (True, "signal")
    assert _needs(
        family_regime_strong=True,
        family_regime_fingerprint="regime:us:up",
        sig=["1h:breakout_up"],
        last_wake_reasons=("regime",),
        last_wake_fingerprints={"regime": "regime:us:up"},
        hours_since_last_llm=1.0,
    ) == (True, "signal")


def test_regime_et_signal_persistants_restent_quiet() -> None:
    assert _needs(
        family_regime_strong=True,
        family_regime_fingerprint="regime:us:up",
        sig=["1h:breakout_up"],
        last_wake_reasons=("regime", "signal"),
        last_wake_fingerprints={
            "regime": "regime:us:up",
            "signal": "signal:1h:breakout_up",
        },
        hours_since_last_llm=1.0,
    ) == (False, "quiet")
    assert _needs(
        family_regime_strong=True,
        family_regime_fingerprint="regime:us:up",
        sig=["1h:breakout_up"],
        last_wake_reasons={"regime", "signal"},
        last_wake_fingerprints={
            "regime": "regime:us:up",
            "signal": "signal:1h:breakout_up",
        },
        hours_since_last_llm=1.5,
    ) == (False, "quiet")
    assert _needs(
        family_regime_strong=True,
        family_regime_fingerprint="regime:us:up",
        sig=["1h:breakout_up"],
        last_wake_reasons=("regime", "signal"),
        last_wake_fingerprints={
            "regime": "regime:us:up",
            "signal": "signal:1h:breakout_up",
        },
        hours_since_last_llm=4.0,
    ) == (True, "regime")


def test_persistent_wake_reasons_seulement_regime_et_signal() -> None:
    assert relevance_gate.persistent_wake_reasons(
        family_regime_strong=True,
        stretched=False,
        sig=None,
        aligned=None,
    ) == ("regime",)
    assert relevance_gate.persistent_wake_reasons(
        family_regime_strong=False,
        stretched=False,
        sig=["1h:breakout_up"],
        aligned=None,
    ) == ("signal",)
    assert relevance_gate.persistent_wake_reasons(
        family_regime_strong=True,
        stretched=True,
        aligned=True,
        sig=["4h:breakout_up"],
    ) == ("regime", "signal")
    assert relevance_gate.persistent_wake_reasons(
        family_regime_strong=False,
        stretched=False,
        sig=["15m:stretched_up"],
        aligned=None,
    ) == ()


def test_agent_wake_et_trigger_urgent_bypassent_le_debounce_position() -> None:
    assert _needs(
        agent_requested_wake=True,
        has_position=True,
        last_wake_reasons=("agent_wake",),
        hours_since_last_llm=0.1,
    ) == (True, "agent_wake")
    assert _needs(
        has_trigger=True,
        has_position=True,
        last_wake_reasons=("trigger",),
        hours_since_last_llm=0.1,
    ) == (True, "trigger")


def test_cockpit_activity_extrait_stretched_et_sig() -> None:
    cockpit = {
        "cols": ["s", "f", "p", "z", "reg", "vs", "st", "cndle", "htf", "aligned", "sig"],
        "rows": [
            ["SPY", "ix", 100.0, 1.2, "range", "low", True, None, "range", False, ["15m:stretched_up"]],
            ["QQQ", "ix", 500.0, 0.1, "range", "low", False, None, "range", False, None],
        ],
    }

    activity = relevance_gate.cockpit_activity(cockpit)

    assert activity["SPY"] == {
        "stretched": True,
        "sig": ["15m:stretched_up"],
        "aligned": False,
        "htf": "range",
    }
    assert activity["QQQ"] == {
        "stretched": False,
        "sig": None,
        "aligned": False,
        "htf": "range",
    }


def test_cockpit_activity_tolere_cockpit_degrade() -> None:
    assert relevance_gate.cockpit_activity({}) == {}
    assert relevance_gate.cockpit_activity({"cols": ["s"], "rows": [["SPY"]]}) == {
        "SPY": {"stretched": None, "sig": None, "aligned": None, "htf": None}
    }


# --- intégration daemon ---


def _flat_bars_factory(now_iso: str):
    from trader.market.market_data import Bar

    def factory(symbol, lookback, interval):
        return [
            Bar(ts=now_iso, open=100.0, high=100.1, low=99.9, close=100.0, volume=1000.0)
            for _ in range(4)
        ]

    return factory


def test_run_cycle_gate_le_polling_calme_sans_appel_llm(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from datetime import datetime, timezone

    from trader.runtime import daemon
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    # le LLM a vu SPY il y a 1h -> revue périodique pas due
    process_state = daemon.CycleProcessState()
    process_state.last_llm_at[(str(state_dir), "SPY")] = now.replace(hour=11)

    batches: list[list[str]] = []

    def decide(**kwargs):
        batches.append(list(kwargs.get("decidable") or [kwargs.get("symbol")]))
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    data_source = make_data_source(_flat_bars_factory(now.isoformat()))

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
        process_state=process_state,
    )

    # aucun appel LLM ; la décision quiet_gate est tracée UNE SEULE fois
    # (pas de doublon "hold" via no_decision_in_batch — review Codex)
    assert batches == [] or all("SPY" not in b for b in batches)
    assert [d["reason"] for d in report["decisions"]] == ["quiet_gate"]
    assert report["decisions"][0]["executed"] is False


def test_run_cycle_debounce_position_routiniere_puis_appelle_a_une_heure(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from datetime import datetime, timedelta, timezone

    from trader.agent.client import Decision
    from trader.execution.broker import Order, SimBroker
    from trader.planning.scheduler import Scheduler
    from trader.runtime import daemon

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, now.isoformat(), dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    process_state = daemon.CycleProcessState()
    process_state.last_llm_at[(str(state_dir), "SPY")] = now - timedelta(minutes=30)
    data_source = make_data_source(_flat_bars_factory(now.isoformat()))
    calls: list[str] = []

    def decide(**kwargs):
        calls.append(kwargs["symbol"])
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="revue de position",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5",
        )

    patch_batch(decide)
    sched = Scheduler(state_dir / "scheduler.json")

    recent_report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        process_state=process_state,
    )

    assert calls == []
    assert recent_report["decisions"][0]["reason"] == "quiet_gate"
    assert recent_report["decisions"][0]["relevance_gate_reason"] == "position_debounce"
    assert recent_report["decisions"][0]["model_called"] is False

    due_at = now + timedelta(hours=1)
    data_source = make_data_source(_flat_bars_factory(due_at.isoformat()))
    due_report = daemon.run_cycle(
        dry_run=True,
        now=due_at,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        process_state=process_state,
    )

    assert calls == ["SPY"]
    assert due_report["decisions"][0]["model_called"] is True
    assert process_state.last_llm_at[(str(state_dir), "SPY")] == due_at


def test_run_cycle_inversion_htf_reveille_position_et_met_a_jour_empreinte(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from datetime import datetime, timedelta, timezone

    from trader.agent.client import Decision
    from trader.execution.broker import Order, SimBroker
    from trader.planning.scheduler import Scheduler
    from trader.runtime import daemon

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    SimBroker(state_dir / "broker.json", starting_cash=100_000).submit(
        Order("SPY", "BUY", 10.0),
        100.0,
        now.isoformat(),
        dry_run=False,
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(
        daemon,
        "build_market_cockpit",
        lambda *args, **kwargs: {
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, ["1h:breakout_down"]]],
        },
    )
    process_state = daemon.CycleProcessState()
    key = (str(state_dir), "SPY")
    process_state.last_llm_at[key] = now - timedelta(minutes=30)
    process_state.last_wake_reasons[key] = ("signal",)
    process_state.last_wake_fingerprints[key] = {
        "signal": "signal:1h:breakout_up"
    }
    calls: list[str] = []

    def decide(**kwargs):
        calls.append(kwargs["symbol"])
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="inversion HTF revue",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5",
        )

    patch_batch(decide)
    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(_flat_bars_factory(now.isoformat())),
        process_state=process_state,
    )

    assert calls == ["SPY"]
    assert report["decisions"][0]["model_called"] is True
    assert process_state.last_wake_fingerprints[key].get("signal") == "signal:1h:breakout_down"
    assert "stale_review" not in process_state.last_wake_fingerprints[key]


def test_run_cycle_disparition_persiste_absence_puis_reapparition_reveille(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from datetime import datetime, timedelta, timezone

    from trader.agent.client import Decision
    from trader.execution.broker import Order, SimBroker
    from trader.planning.scheduler import Scheduler
    from trader.runtime import daemon

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    disappeared_at = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    SimBroker(state_dir / "broker.json", starting_cash=100_000).submit(
        Order("SPY", "BUY", 10.0),
        100.0,
        disappeared_at.isoformat(),
        dry_run=False,
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    current_signal: dict[str, list[str] | None] = {"value": None}
    monkeypatch.setattr(
        daemon,
        "build_market_cockpit",
        lambda *args, **kwargs: {
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, current_signal["value"]]],
        },
    )
    process_state = daemon.CycleProcessState()
    key = (str(state_dir), "SPY")
    process_state.last_llm_at[key] = disappeared_at - timedelta(minutes=30)
    process_state.last_wake_reasons[key] = ("signal",)
    process_state.last_wake_fingerprints[key] = {
        "signal": "signal:1h:breakout_up"
    }
    calls: list[str] = []

    def decide(**kwargs):
        calls.append(kwargs["symbol"])
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="transition HTF revue",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5",
        )

    patch_batch(decide)
    sched = Scheduler(state_dir / "scheduler.json")

    disappeared = daemon.run_cycle(
        dry_run=True,
        now=disappeared_at,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=make_data_source(_flat_bars_factory(disappeared_at.isoformat())),
        process_state=process_state,
    )

    assert calls == ["SPY"]
    assert disappeared["decisions"][0]["model_called"] is True
    assert process_state.last_wake_reasons[key] == ()
    assert process_state.last_wake_fingerprints[key].get("signal") is None
    assert process_state.last_wake_fingerprints[key].get("regime") is None
    assert "stale_review" not in process_state.last_wake_fingerprints[key]

    still_absent_at = disappeared_at + timedelta(minutes=15)
    still_absent = daemon.run_cycle(
        dry_run=True,
        now=still_absent_at,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=make_data_source(_flat_bars_factory(still_absent_at.isoformat())),
        process_state=process_state,
    )

    assert calls == ["SPY"]
    assert still_absent["decisions"][0]["model_called"] is False
    assert still_absent["decisions"][0]["relevance_gate_reason"] == "position_debounce"

    reappeared_at = still_absent_at + timedelta(minutes=15)
    current_signal["value"] = ["1h:breakout_up"]
    reappeared = daemon.run_cycle(
        dry_run=True,
        now=reappeared_at,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=make_data_source(_flat_bars_factory(reappeared_at.isoformat())),
        process_state=process_state,
    )

    assert calls == ["SPY", "SPY"]
    assert reappeared["decisions"][0]["model_called"] is True
    assert process_state.last_wake_fingerprints[key].get("signal") == "signal:1h:breakout_up"
    assert "stale_review" not in process_state.last_wake_fingerprints[key]


def test_run_cycle_revue_post_entry_15m_bypasse_le_debounce_position(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from datetime import datetime, timedelta, timezone

    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler
    from trader.runtime import daemon

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    opened_at = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    review_at = opened_at + timedelta(minutes=15)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    process_state = daemon.CycleProcessState()
    sched = Scheduler(state_dir / "scheduler.json")
    data_source = make_data_source(_flat_bars_factory(opened_at.isoformat()))
    calls: list[str] = []

    def decide(**kwargs):
        calls.append(kwargs["symbol"])
        if len(calls) == 1:
            return Decision(
                symbol="SPY",
                action="BUY",
                quantity=10.0,
                confidence=0.8,
                rationale="entrée avec protection",
                intent="OPEN_LONG",
                exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
                llm_provider="acpx",
                llm_model="gpt-5.5",
            )
        return Decision(
            symbol="SPY",
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="thèse intacte après entrée",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5",
        )

    patch_batch(decide)

    opened = daemon.run_cycle(
        dry_run=False,
        now=opened_at,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        process_state=process_state,
    )

    assert opened["decisions"][0]["post_entry_review_scheduled"] is True
    assert sched.next_wake("SPY") == review_at
    assert process_state.last_llm_at[(str(state_dir), "SPY")] == opened_at

    reviewed = daemon.run_cycle(
        dry_run=False,
        now=review_at,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        process_state=process_state,
    )

    assert calls == ["SPY", "SPY"]
    assert reviewed["decisions"][0]["model_called"] is True
    assert process_state.last_llm_at[(str(state_dir), "SPY")] == review_at


def test_run_cycle_honore_le_reveil_demande_par_l_agent(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from datetime import datetime, timezone

    from trader.runtime import daemon
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    process_state = daemon.CycleProcessState()
    process_state.last_llm_at[(str(state_dir), "SPY")] = now.replace(hour=11)

    sched = Scheduler(state_dir / "scheduler.json")
    # l'agent avait demandé un réveil (échu) -> il doit être honoré malgré le calme
    sched.set_symbol_next_wake_in("SPY", minutes=-5, now=now)

    decided: list[str] = []

    def decide(**kwargs):
        decided.append(kwargs["symbol"])
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    data_source = make_data_source(_flat_bars_factory(now.isoformat()))

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        process_state=process_state,
    )

    assert decided == ["SPY"]


def test_run_cycle_ne_marque_pas_un_echec_llm_comme_revue_periodique(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from datetime import datetime, timezone

    from trader.runtime import daemon
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    process_state = daemon.CycleProcessState()

    def decide(**kwargs):
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.0,
            rationale="llm_failed:acpx:timeout:> 900s",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5",
            llm_error="timeout",
        )

    patch_batch(decide)
    data_source = make_data_source(_flat_bars_factory(now.isoformat()))

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
        process_state=process_state,
    )

    assert (str(state_dir), "SPY") not in process_state.last_llm_at
    assert (str(state_dir), "SPY") not in process_state.last_wake_reasons


def test_run_cycle_stocke_la_raison_de_reveil_sur_revue_llm(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from datetime import datetime, timezone

    from trader.runtime import daemon
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    process_state = daemon.CycleProcessState()

    def decide(**kwargs):
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.4,
            rationale="revue",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5",
        )

    patch_batch(decide)
    data_source = make_data_source(_flat_bars_factory(now.isoformat()))

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
        process_state=process_state,
    )

    key = (str(state_dir), "SPY")
    assert process_state.last_llm_at[key] == now
    assert process_state.last_wake_reasons[key] == ()
