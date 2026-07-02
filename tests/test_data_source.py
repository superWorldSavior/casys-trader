"""Tests : DataSource Protocol, YFinanceDataSource, CompositeDataSource, config."""

import pytest

from trader.tools.market import Bar, MarketError


# --------------------------------------------------------------------------
# Helpers partagés
# --------------------------------------------------------------------------

def _make_bar(ts: str = "2026-06-10T10:00:00+00:00") -> Bar:
    return Bar(ts=ts, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)


# --------------------------------------------------------------------------
# Task 1 — Protocol + YFinanceDataSource
# --------------------------------------------------------------------------

class TestDataSourceProtocol:
    def test_yfinance_source_implementes_protocol(self):
        """YFinanceDataSource doit satisfaire DataSource (duck-typing + isinstance Protocol)."""
        from trader.tools.data_source import DataSource, YFinanceDataSource
        ds = YFinanceDataSource()
        # runtime_checkable → isinstance fonctionne
        assert isinstance(ds, DataSource)

    def test_yfinance_get_bars_appelle_market_get_bars(self, monkeypatch):
        """YFinanceDataSource délègue à market.get_bars sans logique propre."""
        from trader.tools import market
        from trader.tools.data_source import YFinanceDataSource

        bar = _make_bar()
        calls = []

        def fake_get_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
            calls.append((symbol, lookback, interval))
            return [bar]

        monkeypatch.setattr(market, "get_bars", fake_get_bars)
        ds = YFinanceDataSource()
        result = ds.get_bars("SPY", "5d", "1h")

        assert calls == [("SPY", "5d", "1h")]
        assert result == [bar]

    def test_yfinance_get_bars_propage_market_error(self, monkeypatch):
        """MarketError levée par market.get_bars remonte telle quelle."""
        from trader.tools import market
        from trader.tools.data_source import YFinanceDataSource

        def boom(*_args, **_kwargs):
            raise MarketError("no_data", "SPY: rien")

        monkeypatch.setattr(market, "get_bars", boom)
        ds = YFinanceDataSource()
        with pytest.raises(MarketError) as exc_info:
            ds.get_bars("SPY", "5d", "1h")
        assert exc_info.value.code == "no_data"

    def test_ibdatasource_implementes_protocol(self):
        """IBDataSource existant doit satisfaire DataSource (pas de régression)."""
        from trader.tools.data_source import DataSource
        from trader.tools.ib_source import IBDataSource

        class FakeIB:
            def qualifyContracts(self, _): return []
            def reqHistoricalData(self, *_, **__): return []
            def disconnect(self): pass

        ds = IBDataSource(FakeIB())
        assert isinstance(ds, DataSource)


# --------------------------------------------------------------------------
# Task 2 — CompositeDataSource
# --------------------------------------------------------------------------

class TestCompositeDataSource:
    """Routing, fallback exception, fallback stale, source manquante, toutes en échec."""

    def _bar(self, ts: str = "2026-06-10T10:00:00+00:00") -> Bar:
        return Bar(ts=ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)

    def _source(self, bars: list[Bar] | None = None, *, name: str = "fake") -> object:
        """Construit une source fake. bars=None → lève MarketError."""
        class _FakeSource:
            def get_bars(self, symbol, lookback, interval):
                if bars is None:
                    raise MarketError("no_data", f"{symbol}: fake error from {name}")
                return bars
        return _FakeSource()

    def _fresh_bar(self) -> Bar:
        """Barre avec ts récent (quelques minutes avant maintenant) — toujours fraîche."""
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        return Bar(ts=ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)

    def test_route_exacte_utilise_premiere_source_disponible(self):
        """Symbole exact matché → première source de la route est utilisée."""
        from trader.tools.data_source import CompositeDataSource

        bar = self._fresh_bar()
        src_a = self._source([bar], name="a")
        src_b = self._source([bar], name="b")

        routes = [{"symbols": ["SPY"], "sources": ["a", "b"]}]
        composite = CompositeDataSource(routes=routes, sources={"a": src_a, "b": src_b})

        result = composite.get_bars("SPY", "5d", "1h")
        assert result == [bar]
        assert composite.last_source("SPY") == "a"

    def test_route_glob_matche_pattern(self):
        """Pattern glob *.TW matche 2330.TW."""
        from trader.tools.data_source import CompositeDataSource

        bar = self._fresh_bar()
        src = self._source([bar], name="tw")
        routes = [{"symbols": ["*.TW"], "sources": ["tw"]}]
        composite = CompositeDataSource(routes=routes, sources={"tw": src})

        result = composite.get_bars("2330.TW", "5d", "1h")
        assert result == [bar]
        assert composite.last_source("2330.TW") == "tw"

    def test_premiere_route_gagne_sur_catch_all(self):
        """Première route qui matche prend la priorité sur * en fin de liste."""
        from trader.tools.data_source import CompositeDataSource

        bar_a = self._fresh_bar()
        bar_b = self._fresh_bar()
        src_a = self._source([bar_a], name="a")
        src_b = self._source([bar_b], name="b")

        routes = [
            {"symbols": ["EURUSD=X"], "sources": ["a"]},
            {"symbols": ["*"], "sources": ["b"]},
        ]
        composite = CompositeDataSource(routes=routes, sources={"a": src_a, "b": src_b})

        result = composite.get_bars("EURUSD=X", "5d", "1h")
        assert result == [bar_a]

    def test_fallback_sur_exception(self):
        """Si la première source lève MarketError → fallback sur la suivante."""
        from trader.tools.data_source import CompositeDataSource

        bar = self._fresh_bar()
        src_fail = self._source(None, name="fail")
        src_ok = self._source([bar], name="ok")

        routes = [{"symbols": ["*"], "sources": ["fail", "ok"]}]
        composite = CompositeDataSource(routes=routes, sources={"fail": src_fail, "ok": src_ok})

        result = composite.get_bars("SPY", "5d", "1h")
        assert result == [bar]
        assert composite.last_source("SPY") == "ok"

    def test_fallback_sur_stale(self, monkeypatch):
        """Si la première source retourne des barres stale → fallback sur suivante."""
        from trader.tools import market
        from trader.tools.market import Freshness
        from trader.tools.data_source import CompositeDataSource

        bar_stale = self._bar("2024-01-01T00:00:00+00:00")
        bar_fresh = self._fresh_bar()  # ts dynamique, toujours récent
        src_stale = self._source([bar_stale], name="stale")
        src_fresh = self._source([bar_fresh], name="fresh")

        # Monkeypatch assess_freshness : bar_stale → stale, bar_fresh → fresh
        def fake_assess(bars, *, now, max_age_minutes):
            if bars and bars[0].ts.startswith("2024"):
                return Freshness(False, "too_old", 9999.0)
            return Freshness(True, None, 1.0)

        monkeypatch.setattr(market, "assess_freshness", fake_assess)

        routes = [{"symbols": ["*"], "sources": ["stale", "fresh"]}]
        composite = CompositeDataSource(
            routes=routes,
            sources={"stale": src_stale, "fresh": src_fresh},
        )

        result = composite.get_bars("SPY", "5d", "1h")
        assert result == [bar_fresh]
        assert composite.last_source("SPY") == "fresh"

    def test_source_manquante_dans_dict_sautee(self):
        """Source nommée dans route mais absente du dict sources → sautée silencieusement."""
        from trader.tools.data_source import CompositeDataSource

        bar = self._fresh_bar()
        src_ok = self._source([bar], name="ok")

        routes = [{"symbols": ["*"], "sources": ["ghost", "ok"]}]
        composite = CompositeDataSource(routes=routes, sources={"ok": src_ok})
        # "ghost" absent → sauté, "ok" utilisé
        result = composite.get_bars("SPY", "5d", "1h")
        assert result == [bar]
        assert composite.last_source("SPY") == "ok"

    def test_toutes_sources_en_echec_releve_market_error(self):
        """Toutes les sources en échec → MarketError machine-readable (pas d'exception brute)."""
        from trader.tools.data_source import CompositeDataSource

        src_a = self._source(None, name="a")
        src_b = self._source(None, name="b")

        routes = [{"symbols": ["*"], "sources": ["a", "b"]}]
        composite = CompositeDataSource(routes=routes, sources={"a": src_a, "b": src_b})

        with pytest.raises(MarketError) as exc_info:
            composite.get_bars("SPY", "5d", "1h")
        assert exc_info.value.code == "all_sources_failed"
        assert "SPY" in exc_info.value.context

    def test_last_source_retourne_none_si_symbole_jamais_appele(self):
        """last_source(symbol) → None si jamais appelé pour ce symbole."""
        from trader.tools.data_source import CompositeDataSource

        composite = CompositeDataSource(routes=[], sources={})
        assert composite.last_source("UNKNOWN") is None

    def test_symbole_sans_route_levee_market_error(self):
        """Symbole sans aucune route correspondante → MarketError('no_route')."""
        from trader.tools.data_source import CompositeDataSource

        routes = [{"symbols": ["SPY"], "sources": ["a"]}]
        composite = CompositeDataSource(routes=routes, sources={"a": self._source([], name="a")})

        with pytest.raises(MarketError) as exc_info:
            composite.get_bars("UNKNOWN_SYM", "5d", "1h")
        assert exc_info.value.code == "no_route"


# --------------------------------------------------------------------------
# F4 — CompositeDataSource.disconnect()
# --------------------------------------------------------------------------

class TestCompositeDisconnect:
    """disconnect() ferme chaque source unique qui l'expose."""

    def test_disconnect_appelle_disconnect_sur_chaque_source(self):
        """disconnect() doit appeler disconnect() sur les sources qui l'ont."""
        from trader.tools.data_source import CompositeDataSource

        disconnected: list[str] = []

        class _SourceWithDisconnect:
            def __init__(self, name):
                self._name = name
            def get_bars(self, *_a, **_kw):
                return []
            def disconnect(self):
                disconnected.append(self._name)

        class _SourceWithoutDisconnect:
            def get_bars(self, *_a, **_kw):
                return []

        src_a = _SourceWithDisconnect("a")
        src_b = _SourceWithoutDisconnect()
        src_c = _SourceWithDisconnect("c")

        routes = [{"symbols": ["*"], "sources": ["a", "b", "c"]}]
        composite = CompositeDataSource(
            routes=routes,
            sources={"a": src_a, "b": src_b, "c": src_c},
        )

        composite.disconnect()

        assert sorted(disconnected) == ["a", "c"]

    def test_disconnect_deduplique_la_meme_source(self):
        """Une même instance référencée sous deux noms n'est déconnectée qu'une fois."""
        from trader.tools.data_source import CompositeDataSource

        disconnected: list[int] = []

        class _Src:
            def get_bars(self, *_a, **_kw):
                return []
            def disconnect(self):
                disconnected.append(1)

        shared = _Src()
        routes = [
            {"symbols": ["SPY"], "sources": ["x"]},
            {"symbols": ["*"], "sources": ["y"]},
        ]
        composite = CompositeDataSource(
            routes=routes,
            sources={"x": shared, "y": shared},
        )

        composite.disconnect()

        assert len(disconnected) == 1, "disconnect appelé plus d'une fois sur la même instance"

    def test_disconnect_tolerant_si_source_leve(self):
        """disconnect() tolère les exceptions dans les sources (best-effort)."""
        from trader.tools.data_source import CompositeDataSource

        class _Src:
            def get_bars(self, *_a, **_kw):
                return []
            def disconnect(self):
                raise RuntimeError("boom")

        routes = [{"symbols": ["*"], "sources": ["s"]}]
        composite = CompositeDataSource(routes=routes, sources={"s": _Src()})

        # Ne doit pas lever
        composite.disconnect()


# --------------------------------------------------------------------------
# Task 3 — Config data_sources.yaml + load_composite_from_config
# --------------------------------------------------------------------------

class TestDataSourceConfig:
    """Parsing YAML, profils, fail-fast erreurs."""

    def _write_config(self, tmp_path, content: str):
        p = tmp_path / "data_sources.yaml"
        p.write_text(content)
        return p

    def test_charge_profil_paper_et_construit_composite(self, tmp_path, monkeypatch):
        """Profil paper → CompositeDataSource avec les bonnes routes."""
        from trader.tools.data_source import load_composite_from_config, YFinanceDataSource

        config_content = """
profile: paper
profiles:
  paper:
    routes:
      - symbols: [EURUSD=X, USDJPY=X]
        sources: [ib, yfinance]
      - symbols: ["*"]
        sources: [yfinance, ib]
  prod:
    routes:
      - symbols: ["*"]
        sources: [ib]
"""
        cfg_path = self._write_config(tmp_path, config_content)

        class FakeIB:
            def qualifyContracts(self, _): return []
            def reqHistoricalData(self, *_, **__): return []
            def disconnect(self): pass

        from trader.tools.ib_source import IBDataSource
        available_sources = {
            "yfinance": YFinanceDataSource(),
            "ib": IBDataSource(FakeIB()),
        }

        composite, profile_used = load_composite_from_config(
            cfg_path,
            available_sources=available_sources,
        )

        assert profile_used == "paper"
        # Route FX → ib en premier
        route_fx = composite._resolve_route("EURUSD=X")
        assert route_fx == ["ib", "yfinance"]
        # Route default → yfinance en premier
        route_default = composite._resolve_route("SPY")
        assert route_default == ["yfinance", "ib"]

    def test_override_profil_via_argument(self, tmp_path):
        """profile_override='prod' force le profil prod indépendamment du défaut YAML."""
        from trader.tools.data_source import load_composite_from_config, YFinanceDataSource

        config_content = """
profile: paper
profiles:
  paper:
    routes:
      - symbols: ["*"]
        sources: [yfinance]
  prod:
    routes:
      - symbols: ["*"]
        sources: [ib]
"""
        cfg_path = self._write_config(tmp_path, config_content)

        class FakeIB:
            def qualifyContracts(self, _): return []
            def reqHistoricalData(self, *_, **__): return []
            def disconnect(self): pass

        from trader.tools.ib_source import IBDataSource
        available_sources = {
            "yfinance": YFinanceDataSource(),
            "ib": IBDataSource(FakeIB()),
        }
        composite, profile_used = load_composite_from_config(
            cfg_path,
            available_sources=available_sources,
            profile_override="prod",
        )
        assert profile_used == "prod"
        assert composite._resolve_route("SPY") == ["ib"]

    def test_profil_inconnu_leve_market_error(self, tmp_path):
        """Profil inconnu dans le fichier → MarketError('unknown_profile') fail-fast."""
        from trader.tools.data_source import load_composite_from_config, YFinanceDataSource

        config_content = """
profile: inexistant
profiles:
  paper:
    routes:
      - symbols: ["*"]
        sources: [yfinance]
"""
        cfg_path = self._write_config(tmp_path, config_content)
        with pytest.raises(MarketError) as exc_info:
            load_composite_from_config(cfg_path, available_sources={"yfinance": YFinanceDataSource()})
        assert exc_info.value.code == "unknown_profile"

    def test_profil_override_inconnu_leve_market_error(self, tmp_path):
        """Override vers profil inexistant → MarketError('unknown_profile')."""
        from trader.tools.data_source import load_composite_from_config, YFinanceDataSource

        config_content = """
profile: paper
profiles:
  paper:
    routes:
      - symbols: ["*"]
        sources: [yfinance]
"""
        cfg_path = self._write_config(tmp_path, config_content)
        with pytest.raises(MarketError) as exc_info:
            load_composite_from_config(
                cfg_path,
                available_sources={"yfinance": YFinanceDataSource()},
                profile_override="nope",
            )
        assert exc_info.value.code == "unknown_profile"

    def test_source_inconnue_dans_route_leve_market_error(self, tmp_path):
        """Source nommée dans route mais inconnue du registre → MarketError('unknown_source').

        Le registre known_source_names est fourni au démarrage ; alpaca n'y figure pas.
        Cela détecte les fautes de frappe dans la config indépendamment de la connexion.
        """
        from trader.tools.data_source import load_composite_from_config, YFinanceDataSource

        config_content = """
profile: paper
profiles:
  paper:
    routes:
      - symbols: ["*"]
        sources: [yfinance, alpaca]
"""
        cfg_path = self._write_config(tmp_path, config_content)
        with pytest.raises(MarketError) as exc_info:
            load_composite_from_config(
                cfg_path,
                available_sources={"yfinance": YFinanceDataSource()},
                known_source_names=frozenset({"yfinance", "ib"}),
            )
        assert exc_info.value.code == "unknown_source"
        assert "alpaca" in exc_info.value.context

    def test_yaml_invalide_leve_market_error(self, tmp_path):
        """YAML malformé → MarketError('invalid_config')."""
        from trader.tools.data_source import load_composite_from_config

        cfg_path = self._write_config(tmp_path, "{ invalid yaml: [unclosed")
        with pytest.raises(MarketError) as exc_info:
            load_composite_from_config(cfg_path, available_sources={})
        assert exc_info.value.code == "invalid_config"

    def test_fichier_absent_leve_market_error(self, tmp_path):
        """Fichier absent → MarketError('config_not_found')."""
        from trader.tools.data_source import load_composite_from_config

        absent = tmp_path / "absent.yaml"
        with pytest.raises(MarketError) as exc_info:
            load_composite_from_config(absent, available_sources={})
        assert exc_info.value.code == "config_not_found"


# --------------------------------------------------------------------------
# F6 — CompositeDataSource : all-stale retourne le meilleur stale (pas d'exception)
# --------------------------------------------------------------------------

class TestCompositeF6StaleFallback:
    """F6 : toutes sources stale → retourner la plus fraîche ; seulement toutes-exception → all_sources_failed."""

    def _stale_bar(self) -> "Bar":
        return Bar(ts="2024-01-01T00:00:00+00:00", open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)

    def _stale_bar2(self) -> "Bar":
        return Bar(ts="2024-06-01T00:00:00+00:00", open=2.0, high=3.0, low=1.0, close=2.5, volume=20.0)

    def test_all_stale_retourne_meilleur_stale_pas_exception(self, monkeypatch):
        """Toutes sources retournent stale → CompositeDataSource retourne les barres stale (pas d'exception).

        C'est le daemon (couche métier) qui décide du traitement ; le composite
        retourne toujours des barres si au moins une source en a.
        """
        from trader.tools import market
        from trader.tools.market import Freshness
        from trader.tools.data_source import CompositeDataSource

        bar_stale_a = self._stale_bar()   # ts 2024-01-01 — plus vieux
        bar_stale_b = self._stale_bar2()  # ts 2024-06-01 — moins vieux

        class SrcA:
            def get_bars(self, *_a, **_kw):
                return [bar_stale_a]

        class SrcB:
            def get_bars(self, *_a, **_kw):
                return [bar_stale_b]

        def fake_assess(bars, *, now, max_age_minutes):
            # Toutes les barres sont stale
            return Freshness(False, "too_old", 9999.0)

        monkeypatch.setattr(market, "assess_freshness", fake_assess)

        routes = [{"symbols": ["*"], "sources": ["a", "b"]}]
        composite = CompositeDataSource(routes=routes, sources={"a": SrcA(), "b": SrcB()})

        # Ne doit PAS lever — retourne les barres de la première source stale (a)
        result = composite.get_bars("SPY", "5d", "1h")
        # Les barres sont celles de la première source stale tentée
        assert result == [bar_stale_a]
        assert composite.last_source("SPY") == "a"

    def test_mix_stale_exception_retourne_stale(self, monkeypatch):
        """Source 1 lève exception, source 2 retourne stale → retourner les barres stale (pas d'exception)."""
        from trader.tools import market
        from trader.tools.market import Freshness
        from trader.tools.data_source import CompositeDataSource

        bar_stale = self._stale_bar()

        class SrcFail:
            def get_bars(self, *_a, **_kw):
                raise MarketError("no_data", "boom")

        class SrcStale:
            def get_bars(self, *_a, **_kw):
                return [bar_stale]

        def fake_assess(bars, *, now, max_age_minutes):
            return Freshness(False, "too_old", 9999.0)

        monkeypatch.setattr(market, "assess_freshness", fake_assess)

        routes = [{"symbols": ["*"], "sources": ["fail", "stale"]}]
        composite = CompositeDataSource(
            routes=routes, sources={"fail": SrcFail(), "stale": SrcStale()}
        )

        result = composite.get_bars("SPY", "5d", "1h")
        assert result == [bar_stale]
        assert composite.last_source("SPY") == "stale"

    def test_all_exception_leve_all_sources_failed(self):
        """Toutes sources lèvent exception (aucune barre stale) → MarketError('all_sources_failed')."""
        from trader.tools.data_source import CompositeDataSource

        class SrcFail:
            def get_bars(self, *_a, **_kw):
                raise MarketError("no_data", "boom")

        routes = [{"symbols": ["*"], "sources": ["a", "b"]}]
        composite = CompositeDataSource(
            routes=routes, sources={"a": SrcFail(), "b": SrcFail()}
        )

        with pytest.raises(MarketError) as exc_info:
            composite.get_bars("SPY", "5d", "1h")
        assert exc_info.value.code == "all_sources_failed"
