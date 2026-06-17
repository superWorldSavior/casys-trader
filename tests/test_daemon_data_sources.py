"""Tests d'intégration daemon : câblage CompositeDataSource, observabilité data_source."""

import json
from datetime import datetime, timezone

import pytest

from trader import daemon
from trader.tools.market import Bar, MarketError
from trader.tools.scheduler import Scheduler


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
        "max_orders_per_cycle: 5\n"
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
        from trader.tools.market import Bar
        now = datetime.now(timezone.utc)
        bar_ts = (now - timedelta(minutes=5)).isoformat()
        return Bar(ts=bar_ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)

    def test_data_source_present_dans_entry_decision(
        self, monkeypatch, tmp_path
    ):
        """run_cycle : entry de décision doit contenir 'data_source'."""
        from trader.tools.scheduler import Scheduler
        from trader.codex_client import Decision

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
        from trader.tools.scheduler import Scheduler
        from trader.codex_client import Decision

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
        from trader.tools.scheduler import Scheduler
        from trader.tools.market import Freshness

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
        import trader.tools.market as market_mod

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
        from trader.tools.market import Freshness
        from trader.tools.data_source import CompositeDataSource, YFinanceDataSource

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
        import trader.tools.market as market_mod
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
