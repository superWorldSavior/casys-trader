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
        hours_since_last_llm=1.0,
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
    assert _needs(has_position=True) == (True, "position")
    assert _needs(family_regime_strong=True) == (True, "regime")
    assert _needs(sig=["1h:breakout_up"]) == (True, "signal")


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
        last_wake_reasons=("regime",),
        hours_since_last_llm=1.0,
    ) == (False, "quiet")
    assert _needs(
        family_regime_strong=True,
        last_wake_reasons=("regime",),
        hours_since_last_llm=2.0,
    ) == (True, "regime")


def test_signal_htf_debounced_sous_deux_heures() -> None:
    assert _needs(
        sig=["1h:breakout_up"],
        last_wake_reasons=("signal",),
        hours_since_last_llm=1.0,
    ) == (False, "quiet")
    assert _needs(
        stretched=True,
        aligned=True,
        last_wake_reasons=("signal",),
        hours_since_last_llm=1.5,
    ) == (False, "quiet")
    assert _needs(
        sig=["4h:breakout_up"],
        last_wake_reasons=("signal",),
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
        sig=["1h:breakout_up"],
        last_wake_reasons=("regime",),
        hours_since_last_llm=1.0,
    ) == (True, "signal")


def test_regime_et_signal_persistants_restent_quiet() -> None:
    assert _needs(
        family_regime_strong=True,
        sig=["1h:breakout_up"],
        last_wake_reasons=("regime", "signal"),
        hours_since_last_llm=1.0,
    ) == (False, "quiet")
    assert _needs(
        family_regime_strong=True,
        sig=["1h:breakout_up"],
        last_wake_reasons={"regime", "signal"},
        hours_since_last_llm=1.5,
    ) == (False, "quiet")
    assert _needs(
        family_regime_strong=True,
        sig=["1h:breakout_up"],
        last_wake_reasons=("regime", "signal"),
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


def test_agent_wake_trigger_position_jamais_debounced() -> None:
    assert _needs(
        agent_requested_wake=True,
        last_wake_reasons=("agent_wake",),
        hours_since_last_llm=0.1,
    ) == (True, "agent_wake")
    assert _needs(
        has_trigger=True,
        last_wake_reasons=("trigger",),
        hours_since_last_llm=0.1,
    ) == (True, "trigger")
    assert _needs(
        has_position=True,
        last_wake_reasons=("position",),
        hours_since_last_llm=0.1,
    ) == (True, "position")


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


from conftest import write_runtime_config as _runtime_config  # noqa: E402


def _flat_bars_factory(now_iso: str):
    from trader.market.market_data import Bar

    def factory(symbol, lookback, interval):
        return [
            Bar(ts=now_iso, open=100.0, high=100.1, low=99.9, close=100.0, volume=1000.0)
            for _ in range(4)
        ]

    return factory


def test_run_cycle_gate_le_polling_calme_sans_appel_llm(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    from datetime import datetime, timezone

    from trader.runtime import daemon
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    _runtime_config(tmp_path)
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


def test_run_cycle_honore_le_reveil_demande_par_l_agent(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    from datetime import datetime, timezone

    from trader.runtime import daemon
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    _runtime_config(tmp_path)
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
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    from datetime import datetime, timezone

    from trader.runtime import daemon
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    _runtime_config(tmp_path)
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
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    from datetime import datetime, timezone

    from trader.runtime import daemon
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    _runtime_config(tmp_path)
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
