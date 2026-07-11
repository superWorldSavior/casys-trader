"""Tests d'intégration daemon : câblage CompositeDataSource, observabilité data_source."""

from datetime import datetime, timezone

import pytest

from trader.runtime import daemon
from trader.runtime import cycle_dispatch
from trader.runtime import data_source_runtime
from trader.runtime import market_rotation_runtime
from trader.runtime import news_macro_runtime
from trader.runtime import universe_intelligence_runtime
from trader.runtime.worker_cycle_context import WorkerCycleContextHandle
from trader.market.market_data import Bar, MarketError
from trader.planning.scheduler import Scheduler


# ---------------------------------------------------------------------------
# Helpers partagés
# ---------------------------------------------------------------------------

def _write_runtime_config(root) -> None:
    (root / "config").mkdir(exist_ok=True)
    (root / "mandate").mkdir(exist_ok=True)
    (root / "config" / "universe.yaml").write_text(
        "starting_cash: 100000\nsymbols:\n  - SPY\n"
    )
    (root / "config" / "risk.yaml").write_text(
        "max_position_value: 20000\n"
        "max_gross_exposure: 100000\n"
        "max_order_value: 10000\n"
        "min_equity: 50000\n"
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def _write_data_sources_config(root, profile: str = "paper") -> None:
    """Crée config/data_sources.yaml minimal dans root."""
    content = f"""profile: {profile}
profiles:
  paper:
    routes:
      - symbols: ["*"]
        sources: [yfinance, ib]
  prod:
    routes:
      - symbols: ["*"]
        sources: [ib]
"""
    (root / "config" / "data_sources.yaml").write_text(content)


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDaemonDataSourcesConfig:
    """Config présente → composite utilisé ; config absente → IB direct."""

    def test_main_delegue_le_bootstrap_data_source_au_runtime_module(
        self, monkeypatch, tmp_path
    ):
        """main() garde l'orchestration, le bootstrap concret vit dans runtime/data_source_runtime."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        build_calls: list[dict] = []
        sources_seen: list[object] = []

        class FakeSource:
            def disconnect(self):
                pass

        delegated_source = FakeSource()

        def build_data_source(config, **kwargs):
            build_calls.append({"config": config, "kwargs": kwargs})
            return data_source_runtime.DataSourceState(
                data_source=delegated_source,
                composite_available={},
                ib_attach_backoff=None,
            )

        def run_cycle(**kwargs):
            sources_seen.append(kwargs["data_source"])
            return _empty_report(now)

        class FakeIB:
            def disconnect(self):
                pass

        class FakeIBDS:
            def __init__(self, ib, *, reconnect_factory=None):
                self.ib = ib

            def disconnect(self):
                self.ib.disconnect()

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "data_source_runtime", data_source_runtime, raising=False)
        monkeypatch.setattr(data_source_runtime, "build_data_source", build_data_source)
        monkeypatch.setattr(daemon, "connect_ib", lambda *_args, **_kwargs: FakeIB(), raising=False)
        monkeypatch.setattr(daemon, "IBDataSource", FakeIBDS, raising=False)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)

        daemon.main(["--once"])

        assert sources_seen == [delegated_source]
        assert len(build_calls) == 1
        assert build_calls[0]["config"] == data_source_runtime.DataSourceRuntimeConfig(
            use_composite=False,
            routes=[],
            profile="",
        )
        assert build_calls[0]["kwargs"]["host"] == "127.0.0.1"
        assert build_calls[0]["kwargs"]["port"] == 4002

    def test_main_once_ne_lance_pas_le_runner_news_macro_asynchrone(
        self, monkeypatch, tmp_path
    ):
        """Un daemon --once ne lance pas un thread LLM qu'il tuerait en sortant."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        rotation_calls: list[dict] = []
        runner_calls: list[dict | str] = []
        universe_runner_calls: list[dict | str] = []

        class FakeSource:
            def disconnect(self):
                pass

        def build_data_source(_config, **_kwargs):
            return data_source_runtime.DataSourceState(
                data_source=FakeSource(),
                composite_available={},
                ib_attach_backoff=None,
            )

        def run_cycle(**_kwargs):
            return _empty_report(now)

        def tick_market_rotation(**kwargs):
            rotation_calls.append(kwargs)

        class FakeNewsMacroRunner:
            def trigger(self, **kwargs):
                runner_calls.append(kwargs)

            def stop(self):
                runner_calls.append("stop")

        class FakeUniverseIntelligenceRunner:
            def trigger(self, **kwargs):
                universe_runner_calls.append(kwargs)

            def stop(self):
                universe_runner_calls.append("stop")

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "data_source_runtime", data_source_runtime, raising=False)
        monkeypatch.setattr(data_source_runtime, "build_data_source", build_data_source)
        monkeypatch.setattr(daemon, "market_rotation_runtime", market_rotation_runtime, raising=False)
        monkeypatch.setattr(daemon, "news_macro_runtime", news_macro_runtime, raising=False)
        monkeypatch.setattr(news_macro_runtime, "NewsMacroAnalysisRunner", FakeNewsMacroRunner)
        monkeypatch.setattr(
            daemon,
            "universe_intelligence_runtime",
            universe_intelligence_runtime,
            raising=False,
        )
        monkeypatch.setattr(
            universe_intelligence_runtime,
            "UniverseIntelligenceRunner",
            FakeUniverseIntelligenceRunner,
        )
        monkeypatch.setattr(market_rotation_runtime, "tick_market_rotation", tick_market_rotation)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)

        daemon.main(["--once"], now_fn=lambda: now)

        assert len(rotation_calls) == 1
        assert rotation_calls[0]["config_dir"] == tmp_path / "config"
        assert rotation_calls[0]["state_dir"] == state_dir
        assert rotation_calls[0]["loop_now"] == now
        assert rotation_calls[0]["logger"] is daemon.log
        assert runner_calls == ["stop"]
        assert universe_runner_calls == ["stop"]

    def test_main_delegue_run_cycle_au_runtime_dispatcher(
        self, monkeypatch, tmp_path
    ):
        """main() prépare le contexte runtime, cycle_dispatch possède l'appel run_cycle."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        dispatch_calls: list[dict] = []
        tool_service_calls: list[dict] = []
        queue_runtime_calls: list[dict] = []

        class FakeSource:
            def disconnect(self):
                pass

        delegated_source = FakeSource()
        tool_services = object()

        def build_data_source(_config, **_kwargs):
            return data_source_runtime.DataSourceState(
                data_source=delegated_source,
                composite_available={},
                ib_attach_backoff=None,
            )

        def run_cycle(**_kwargs):
            raise AssertionError("run_cycle doit passer par cycle_dispatch")

        def dispatch_run_cycle(**kwargs):
            dispatch_calls.append(kwargs)
            return _empty_report(now)

        def build_decide_tool_services(**kwargs):
            tool_service_calls.append(kwargs)
            return tool_services

        def start_queue_runtimes(**kwargs):
            queue_runtime_calls.append(kwargs)
            return daemon.queue_runtime.QueueRuntimes(
                decide=daemon.queue_runtime.DecideQueueRuntime(enabled=True),
                execute=daemon.queue_runtime.ExecuteQueueRuntime(enabled=True),
            )

        monkeypatch.setenv("CASYS_QUEUE_DECIDE_ENABLED", "1")
        monkeypatch.setenv("CASYS_QUEUE_EXECUTE_ENABLED", "1")
        monkeypatch.setenv("CASYS_AGENT_TOOLS_ENABLED", "1")
        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "data_source_runtime", data_source_runtime, raising=False)
        monkeypatch.setattr(data_source_runtime, "build_data_source", build_data_source)
        monkeypatch.setattr(daemon, "market_rotation_runtime", market_rotation_runtime, raising=False)
        monkeypatch.setattr(market_rotation_runtime, "tick_market_rotation", lambda **_kwargs: None)
        monkeypatch.setattr(daemon, "cycle_dispatch", cycle_dispatch, raising=False)
        monkeypatch.setattr(cycle_dispatch, "dispatch_run_cycle", dispatch_run_cycle)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)
        monkeypatch.setattr(daemon.queue_runtime, "build_decide_tool_services", build_decide_tool_services)
        monkeypatch.setattr(daemon.queue_runtime, "start_queue_runtimes", start_queue_runtimes)

        daemon.main(["--once"], now_fn=lambda: now)

        assert len(tool_service_calls) == 1
        tool_call = tool_service_calls[0]
        assert isinstance(tool_call["worker_cycle_context"], WorkerCycleContextHandle)
        assert queue_runtime_calls[0]["decide_tool_services"] is tool_services
        assert len(dispatch_calls) == 1
        call = dispatch_calls[0]
        assert callable(call["run_cycle_fn"])
        assert call["now"] == now
        assert call["symbols_filter"] == ["SPY"]
        context = call["context"]
        assert context.dry_run is True
        assert context.data_source is delegated_source
        assert context.queue_decide_enabled is True
        assert context.queue_execute_enabled is True
        assert context.agent_tools_enabled is True
        assert context.worker_cycle_context is tool_call["worker_cycle_context"]

    def test_main_ne_dispatch_pas_de_cycle_quand_aucun_symbole_n_est_du(
        self, monkeypatch, tmp_path
    ):
        """Si le scheduler dort, la boucle surveille les veilles sans lancer de cycle vide."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        Scheduler(state_dir / "scheduler.json").set_symbol_next_wake(
            "SPY",
            "2026-06-10T13:00:00+00:00",
        )
        dispatch_calls: list[dict] = []
        sleeps: list[float] = []
        runner_calls: list[dict | str] = []
        universe_runner_calls: list[dict | str] = []

        class FakeSource:
            def disconnect(self):
                pass

        delegated_source = FakeSource()

        class FakeNewsMacroRunner:
            def trigger(self, **kwargs):
                runner_calls.append(kwargs)

            def stop(self):
                runner_calls.append("stop")

        class FakeUniverseIntelligenceRunner:
            def trigger(self, **kwargs):
                universe_runner_calls.append(kwargs)

            def stop(self):
                universe_runner_calls.append("stop")

        def build_data_source(_config, **_kwargs):
            return data_source_runtime.DataSourceState(
                data_source=delegated_source,
                composite_available={},
                ib_attach_backoff=None,
            )

        def dispatch_run_cycle(**kwargs):
            dispatch_calls.append(kwargs)
            return _empty_report(now)

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            raise KeyboardInterrupt

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "data_source_runtime", data_source_runtime, raising=False)
        monkeypatch.setattr(data_source_runtime, "build_data_source", build_data_source)
        monkeypatch.setattr(daemon, "market_rotation_runtime", market_rotation_runtime, raising=False)
        monkeypatch.setattr(daemon, "news_macro_runtime", news_macro_runtime, raising=False)
        monkeypatch.setattr(news_macro_runtime, "NewsMacroAnalysisRunner", FakeNewsMacroRunner)
        monkeypatch.setattr(
            daemon,
            "universe_intelligence_runtime",
            universe_intelligence_runtime,
            raising=False,
        )
        monkeypatch.setattr(
            universe_intelligence_runtime,
            "UniverseIntelligenceRunner",
            FakeUniverseIntelligenceRunner,
        )
        monkeypatch.setattr(market_rotation_runtime, "tick_market_rotation", lambda **_kwargs: None)
        monkeypatch.setattr(daemon, "cycle_dispatch", cycle_dispatch, raising=False)
        monkeypatch.setattr(cycle_dispatch, "dispatch_run_cycle", dispatch_run_cycle)
        monkeypatch.setattr(daemon.time, "sleep", sleep)

        with pytest.raises(KeyboardInterrupt):
            daemon.main(["--poll", "0.01"], now_fn=lambda: now)

        assert dispatch_calls == []
        assert sleeps == [0.01]
        assert len(runner_calls) == 2
        assert runner_calls[0]["config_dir"] == tmp_path / "config"
        assert runner_calls[0]["state_dir"] == state_dir
        assert runner_calls[0]["loop_now"] == now
        assert runner_calls[1] == "stop"
        assert len(universe_runner_calls) == 2
        assert universe_runner_calls[0]["config_dir"] == tmp_path / "config"
        assert universe_runner_calls[0]["state_dir"] == state_dir
        assert universe_runner_calls[0]["loop_now"] == now
        assert universe_runner_calls[1] == "stop"

    def test_config_presente_construit_composite_et_passe_au_cycle(
        self, monkeypatch, tmp_path
    ):
        """Si data_sources.yaml existe → CompositeDataSource passé à run_cycle."""
        _write_runtime_config(tmp_path)
        _write_data_sources_config(tmp_path, profile="paper")
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        sources_used: list[str] = []

        def run_cycle(**kwargs):
            sources_used.append(type(kwargs["data_source"]).__name__)
            return _empty_report(now)

        # IB disponible (ne doit pas lever)
        class FakeIB:
            def disconnect(self): pass

        def connect_ib(*_a, **_kw):
            return FakeIB()

        class FakeIBDS:
            def __init__(self, ib, *, reconnect_factory=None): pass
            def disconnect(self): pass

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
        monkeypatch.setattr(daemon, "IBDataSource", FakeIBDS, raising=False)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)

        daemon.main(["--once"])

        assert sources_used == ["CompositeDataSource"]

    def test_config_absente_utilise_ibdatasource_direct(
        self, monkeypatch, tmp_path
    ):
        """Si data_sources.yaml absent → IBDataSource direct (rétrocompat)."""
        _write_runtime_config(tmp_path)
        # PAS de data_sources.yaml
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        sources_used: list[str] = []

        def run_cycle(**kwargs):
            sources_used.append(type(kwargs["data_source"]).__name__)
            return _empty_report(now)

        class FakeIB:
            def disconnect(self): pass

        def connect_ib(*_a, **_kw):
            return FakeIB()

        class FakeIBDS:
            def __init__(self, ib, *, reconnect_factory=None): pass
            def disconnect(self): pass

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
        monkeypatch.setattr(daemon, "IBDataSource", FakeIBDS, raising=False)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)

        daemon.main(["--once"])

        assert sources_used == ["FakeIBDS"]

    def test_profil_override_cli(self, monkeypatch, tmp_path):
        """--data-profile prod → parse_data_sources_config reçoit profile_override='prod'."""
        _write_runtime_config(tmp_path)
        _write_data_sources_config(tmp_path, profile="paper")
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        profiles_used: list[str] = []

        # Patcher daemon.parse_data_sources_config (appelé avant la boucle)
        original_parse = daemon.parse_data_sources_config

        def fake_parse(cfg_path, *, profile_override=None, known_source_names=None):
            profiles_used.append(profile_override)
            return original_parse(cfg_path, profile_override=profile_override, known_source_names=known_source_names)

        monkeypatch.setattr(daemon, "parse_data_sources_config", fake_parse)

        def run_cycle(**kwargs):
            return _empty_report(now)

        class FakeIB:
            def disconnect(self): pass

        def connect_ib(*_a, **_kw):
            return FakeIB()

        class FakeIBDS:
            def __init__(self, ib, *, reconnect_factory=None): pass
            def disconnect(self): pass

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
        monkeypatch.setattr(daemon, "IBDataSource", FakeIBDS, raising=False)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)

        daemon.main(["--once", "--data-profile", "prod"])

        assert profiles_used == ["prod"]

    def test_profil_override_env(self, monkeypatch, tmp_path):
        """TRADER_DATA_PROFILE=prod → parse_data_sources_config reçoit profile_override='prod'."""
        _write_runtime_config(tmp_path)
        _write_data_sources_config(tmp_path, profile="paper")
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        monkeypatch.setenv("TRADER_DATA_PROFILE", "prod")
        profiles_used: list[str] = []

        original_parse = daemon.parse_data_sources_config

        def fake_parse(cfg_path, *, profile_override=None, known_source_names=None):
            profiles_used.append(profile_override)
            return original_parse(cfg_path, profile_override=profile_override, known_source_names=known_source_names)

        monkeypatch.setattr(daemon, "parse_data_sources_config", fake_parse)

        def run_cycle(**kwargs):
            return _empty_report(now)

        class FakeIB:
            def disconnect(self): pass

        def connect_ib(*_a, **_kw):
            return FakeIB()

        class FakeIBDS:
            def __init__(self, ib, *, reconnect_factory=None): pass
            def disconnect(self): pass

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
        monkeypatch.setattr(daemon, "IBDataSource", FakeIBDS, raising=False)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)

        daemon.main(["--once"])

        assert profiles_used == ["prod"]

    def test_ib_down_en_profil_paper_ne_saute_pas_le_cycle(
        self, monkeypatch, tmp_path
    ):
        """IB down + profil paper + config présente → cycle NE DOIT PAS être sauté."""
        _write_runtime_config(tmp_path)
        _write_data_sources_config(tmp_path, profile="paper")
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        cycles_run: list[bool] = []

        def run_cycle(**kwargs):
            cycles_run.append(True)
            return _empty_report(now)

        def connect_ib(*_a, **_kw):
            raise MarketError("ib_connect_failed", "IB down test")

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)

        daemon.main(["--once"])

        assert cycles_run == [True], "cycle doit tourner même IB down en profil paper"


class TestDaemonMarketDataType:
    """F1 — market_data_type câblé au profil : prod→1, paper/rétrocompat→3."""

    def _base(self, tmp_path, monkeypatch, profile: str):
        _write_runtime_config(tmp_path)
        _write_data_sources_config(tmp_path, profile=profile)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        mdts: list[int] = []

        def connect_ib(*_a, market_data_type=3, **_kw):
            mdts.append(market_data_type)

            class FakeIB:
                def disconnect(self): pass
            return FakeIB()

        class FakeIBDS:
            def __init__(self, ib, *, reconnect_factory=None): pass
            def disconnect(self): pass

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
        monkeypatch.setattr(daemon, "IBDataSource", FakeIBDS, raising=False)
        monkeypatch.setattr(daemon, "run_cycle", lambda **_: _empty_report(now))
        return mdts

    def test_profil_prod_utilise_market_data_type_1(self, monkeypatch, tmp_path):
        """Profil prod → connect_ib reçoit market_data_type=1."""
        mdts = self._base(tmp_path, monkeypatch, profile="prod")
        daemon.main(["--once"])
        assert mdts == [1], f"attendu [1], reçu {mdts}"

    def test_profil_paper_utilise_market_data_type_3(self, monkeypatch, tmp_path):
        """Profil paper → connect_ib reçoit market_data_type=3."""
        mdts = self._base(tmp_path, monkeypatch, profile="paper")
        daemon.main(["--once"])
        assert mdts == [3], f"attendu [3], reçu {mdts}"

    def test_retrocompat_sans_config_utilise_market_data_type_3(
        self, monkeypatch, tmp_path
    ):
        """Mode rétrocompat (sans config) → connect_ib reçoit market_data_type=3."""
        _write_runtime_config(tmp_path)
        # pas de data_sources.yaml
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        mdts: list[int] = []

        def connect_ib(*_a, market_data_type=3, **_kw):
            mdts.append(market_data_type)

            class FakeIB:
                def disconnect(self): pass
            return FakeIB()

        class FakeIBDS:
            def __init__(self, ib, *, reconnect_factory=None): pass
            def disconnect(self): pass

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "connect_ib", connect_ib, raising=False)
        monkeypatch.setattr(daemon, "IBDataSource", FakeIBDS, raising=False)
        monkeypatch.setattr(daemon, "run_cycle", lambda **_: _empty_report(now))

        daemon.main(["--once"])
        assert mdts == [3], f"attendu [3], reçu {mdts}"


class TestDaemonProdIBObligatoire:
    """F2 — profil prod + IB down → cycle sauté (pas de composite dégradé)."""

    def test_prod_ib_down_saute_le_cycle(self, monkeypatch, tmp_path):
        """Profil prod + connect_ib lève → cycle sauté, pas de run_cycle."""
        _write_runtime_config(tmp_path)
        _write_data_sources_config(tmp_path, profile="prod")
        state_dir = tmp_path / "state"
        sleeps: list[float] = []
        cycles_run: list[bool] = []

        def connect_ib(*_a, **_kw):
            raise MarketError("ib_connect_failed", "prod IB down test")

        def run_cycle(**_kwargs):
            cycles_run.append(True)
            return _empty_report(datetime.now(timezone.utc))

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

        assert cycles_run == [], "run_cycle ne doit pas tourner si IB est down en prod"
        assert sleeps, "le daemon doit avoir attendu avant de réessayer"


class TestDaemonConfigFailFast:
    """F3 — config invalide au démarrage → crash explicite avant la boucle."""

    def test_config_invalide_fait_crasher_main_avant_la_boucle(
        self, monkeypatch, tmp_path
    ):
        """YAML invalide → main() lève MarketError sans entrer dans la boucle."""
        _write_runtime_config(tmp_path)
        (tmp_path / "config" / "data_sources.yaml").write_text("{ invalid yaml: [unclosed")
        state_dir = tmp_path / "state"

        cycles_run: list[bool] = []

        def run_cycle(**_kwargs):
            cycles_run.append(True)
            return _empty_report(datetime.now(timezone.utc))

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "run_cycle", run_cycle)

        with pytest.raises(MarketError) as exc_info:
            daemon.main(["--once"])

        assert exc_info.value.code == "invalid_config"
        assert cycles_run == [], "run_cycle ne doit pas être appelé si la config est invalide"

    def test_profil_inconnu_fait_crasher_main(self, monkeypatch, tmp_path):
        """Profil inconnu → MarketError('unknown_profile') au démarrage."""
        _write_runtime_config(tmp_path)
        (tmp_path / "config" / "data_sources.yaml").write_text(
            "profile: nope\nprofiles:\n  paper:\n    routes: []\n"
        )
        state_dir = tmp_path / "state"

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        with pytest.raises(MarketError) as exc_info:
            daemon.main(["--once"])

        assert exc_info.value.code == "unknown_profile"


class TestDaemonDecisionDataSourceField:
    """Le champ data_source est enregistré dans chaque entrée de décision."""

    def _make_fresh_bar(self):
        from datetime import timedelta
        from trader.market.market_data import Bar
        now = datetime.now(timezone.utc)
        bar_ts = (now - timedelta(minutes=5)).isoformat()
        return Bar(ts=bar_ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)

    def test_data_source_present_dans_entry_decision(
        self, monkeypatch, tmp_path
    ):
        """run_cycle : entry de décision doit contenir 'data_source'."""
        from trader.planning.scheduler import Scheduler
        from trader.agent.client import Decision

        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"

        bar = self._make_fresh_bar()

        class TrackingDataSource:
            """Source fake qui expose last_source()."""
            def get_bars(self, symbol, lookback, interval):
                return [bar]
            def last_source(self, symbol: str) -> str | None:
                return "yfinance"

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        def fake_batch(**kwargs):
            return {
                sym: Decision(
                    symbol=sym,
                    action="HOLD",
                    quantity=0,
                    confidence=0.5,
                    rationale="test",
                    next_wake_in_minutes=None,
                    context_request=None,
                    intent="HOLD",
                    llm_provider=None,
                    llm_model=None,
                    llm_fallback_reason=None,
                    llm_error=None,
                    learning=None,
                    indicator_watch=None,
                    exit_plan=None,
                )
                for sym in kwargs["symbols"]
            }

        monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

        report = daemon.run_cycle(
            dry_run=True,
            symbols_filter=["SPY"],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=TrackingDataSource(),
        )

        decisions = report.get("decisions", [])
        assert decisions, "aucune décision dans le rapport"
        decision_spy = next((d for d in decisions if d.get("symbol") == "SPY"), None)
        assert decision_spy is not None, "décision SPY absente"
        assert "data_source" in decision_spy, f"champ data_source manquant: {decision_spy.keys()}"
        assert decision_spy["data_source"] == "yfinance"

    def test_fetch_daily_intercale_ne_change_pas_data_source_capturee(
        self, monkeypatch, tmp_path
    ):
        """F5 : le fetch daily (ou tout autre fetch secondaire) ne doit pas écraser
        la valeur last_source capturée après le fetch runtime décisionnel."""
        from trader.planning.scheduler import Scheduler
        from trader.agent.client import Decision

        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"

        bar = self._make_fresh_bar()
        call_count = [0]

        class RotatingDataSource:
            """last_source retourne 'ib' au premier appel, 'yfinance' aux suivants."""
            def get_bars(self, symbol, lookback, interval):
                call_count[0] += 1
                return [bar]
            def last_source(self, symbol: str) -> str | None:
                # Premier appel (runtime) → "ib" ; les suivants (daily, watches) → "yfinance"
                return "ib" if call_count[0] <= 1 else "yfinance"

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        def fake_batch(**kwargs):
            return {
                sym: Decision(
                    symbol=sym,
                    action="HOLD",
                    quantity=0,
                    confidence=0.5,
                    rationale="test",
                    next_wake_in_minutes=None,
                    context_request=None,
                    intent="HOLD",
                    llm_provider=None,
                    llm_model=None,
                    llm_fallback_reason=None,
                    llm_error=None,
                    learning=None,
                    indicator_watch=None,
                    exit_plan=None,
                )
                for sym in kwargs["symbols"]
            }

        monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

        report = daemon.run_cycle(
            dry_run=True,
            symbols_filter=["SPY"],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=RotatingDataSource(),
        )

        decisions = report.get("decisions", [])
        decision_spy = next((d for d in decisions if d.get("symbol") == "SPY"), None)
        assert decision_spy is not None, "décision SPY absente"
        # La capture doit refléter le premier fetch (runtime), pas les suivants
        assert decision_spy["data_source"] == "ib", (
            f"data_source doit être 'ib' (fetch runtime), reçu '{decision_spy['data_source']}'"
        )

    def test_stale_market_data_inclut_data_source(
        self, monkeypatch, tmp_path
    ):
        """F5 : décision HOLD stale doit aussi contenir 'data_source'."""
        from trader.planning.scheduler import Scheduler
        from trader.domain.market.sessions import Freshness

        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"

        bar = self._make_fresh_bar()

        class StaleDataSource:
            """Retourne des barres ; last_source() → 'yfinance'."""
            def get_bars(self, symbol, lookback, interval):
                return [bar]
            def last_source(self, symbol: str) -> str | None:
                return "yfinance"

        # Forcer assess_freshness à retourner stale pour le fetch runtime
        from trader.domain.market import sessions as market_mod

        def fake_assess_stale(bars, *, now, max_age_minutes):
            return Freshness(False, "too_old", 9999.0)

        # Daily AUSSI stale (§13.4) : sinon un daily frais rendrait le symbole
        # analysable au lieu de produire le HOLD synthétique stale testé ici.
        def fake_assess_daily_stale(bars, *, now, symbol=None):
            return Freshness(False, "stale_session", None)

        monkeypatch.setattr(market_mod, "assess_freshness", fake_assess_stale)
        monkeypatch.setattr(market_mod, "assess_daily_freshness", fake_assess_daily_stale)
        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon.codex_client, "decide_batch", lambda **_: {})

        report = daemon.run_cycle(
            dry_run=True,
            symbols_filter=["SPY"],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=StaleDataSource(),
        )

        decisions = report.get("decisions", [])
        stale_decision = next(
            (d for d in decisions if d.get("symbol") == "SPY" and d.get("rationale") == "stale_market_data"),
            None,
        )
        assert stale_decision is not None, f"décision stale SPY absente: {decisions}"
        assert "data_source" in stale_decision, (
            f"champ data_source absent de la décision stale: {stale_decision.keys()}"
        )


# ---------------------------------------------------------------------------
# F6 — all-stale composite → HOLD stale (symbole pas silencieusement disparu)
# ---------------------------------------------------------------------------

class TestDaemonCompositeF6AllStale:
    """F6 : composite all-stale retourne les barres ; daemon enregistre HOLD stale."""

    def _make_stale_bar(self):
        return Bar(
            ts="2024-01-01T00:00:00+00:00",
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
        )

    def test_composite_all_stale_enregistre_hold_stale(self, monkeypatch, tmp_path):
        """F6 : quand composite retourne barres stale, daemon enregistre HOLD stale — pas de disparition silencieuse.

        Avant F6, CompositeDataSource levait all_sources_failed sur all-stale, et
        le daemon swallowait l'exception → le symbole disparaissait sans trace.
        """
        from trader.domain.market.sessions import Freshness
        from trader.market.data_source import CompositeDataSource

        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"

        bar = self._make_stale_bar()

        # YFinanceDataSource qui retourne des barres stale
        class StaleYF:
            def get_bars(self, symbol, lookback, interval):
                return [bar]

        # Composite routant tout vers StaleYF
        composite = CompositeDataSource(
            routes=[{"symbols": ["*"], "sources": ["yf"]}],
            sources={"yf": StaleYF()},
        )

        # Forcer assess_freshness à dire stale pour toutes les barres
        from trader.domain.market import sessions as market_mod
        monkeypatch.setattr(
            market_mod,
            "assess_freshness",
            lambda bars, *, now, max_age_minutes: Freshness(False, "too_old", 9999.0),
        )
        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon.codex_client, "decide_batch", lambda **_: {})

        report = daemon.run_cycle(
            dry_run=True,
            symbols_filter=["SPY"],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=composite,
        )

        decisions = report.get("decisions", [])
        stale_decision = next(
            (d for d in decisions if d.get("symbol") == "SPY"),
            None,
        )
        assert stale_decision is not None, (
            f"SPY silencieusement disparu — décisions: {decisions}"
        )
        assert stale_decision.get("rationale") == "stale_market_data", (
            f"rationale attendue 'stale_market_data', reçu: {stale_decision.get('rationale')}"
        )
