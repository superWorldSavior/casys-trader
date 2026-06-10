# Data Source Abstraction P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Abstraire les sources de données marché derrière un `DataSource` Protocol, un adapter yfinance, un `CompositeDataSource` routé par config YAML, et câbler le tout dans le daemon avec observabilité `data_source` par décision.

**Architecture:** `trader/tools/data_source.py` contient le Protocol, `YFinanceDataSource` (wrapper mince de `market.get_bars`), et `CompositeDataSource` (routing glob+fallback). `config/data_sources.yaml` déclare les profils paper/prod. `trader/daemon.py` charge la config au démarrage et construit un `CompositeDataSource` ; si la config est absente → comportement IB direct inchangé.

**Tech Stack:** Python 3.12, `typing.Protocol`, `fnmatch`, `yaml`, `pytest`, `monkeypatch`. Pas de nouvelles dépendances PyPI.

---

## Structure des fichiers

| Fichier | Action | Responsabilité |
|---|---|---|
| `trader/tools/data_source.py` | **Créer** | Protocol `DataSource`, `YFinanceDataSource`, `CompositeDataSource`, `load_composite_from_config` |
| `config/data_sources.yaml` | **Créer** | Config routes profils paper/prod |
| `tests/test_data_source.py` | **Créer** | Tests Protocol, adapter, composite, config |
| `tests/test_daemon_data_sources.py` | **Créer** | Tests intégration daemon : config présente/absente, `data_source` dans décision |
| `trader/daemon.py` | **Modifier** | Charger config, construire composite, passer `--data-profile`, enregistrer `data_source` dans `entry` |

---

## Task 1 : Protocol `DataSource` + adapter `YFinanceDataSource`

**Files:**
- Create: `trader/tools/data_source.py`
- Test: `tests/test_data_source.py`

### Contexte à lire d'abord
- `trader/tools/market.py:17` — `class Bar` (dataclass frozen)
- `trader/tools/market.py:93` — `class MarketError(code, context)`
- `trader/tools/market.py:234` — signature `get_bars(symbol, lookback="5d", interval="1h") -> list[Bar]`
- `trader/tools/ib_source.py:81` — `class IBDataSource` (exemple d'implémentation existante)

- [ ] **Étape 1.1 : Écrire les tests rouges (Protocol + YFinanceDataSource)**

```python
# tests/test_data_source.py
"""Tests : DataSource Protocol, YFinanceDataSource, CompositeDataSource, config."""

import pytest
from unittest.mock import patch

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
```

- [ ] **Étape 1.2 : Vérifier rouge**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_data_source.py::TestDataSourceProtocol -v 2>&1 | head -30
```

Attendu : `ERROR` ou `ImportError` sur `data_source` module inexistant.

- [ ] **Étape 1.3 : Créer `trader/tools/data_source.py` avec Protocol + YFinanceDataSource**

```python
"""data_source — Protocol DataSource + adapters (yfinance, composite).

AX / Contrats étroits  : Protocol minimal (get_bars seul).
AX / Explicit over Implicit : aucun défaut magique de source.
AX / Machine-Readable Errors : toutes les erreurs sont MarketError(code, context).
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Protocol, runtime_checkable

from trader.tools.market import Bar, MarketError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DataSource(Protocol):
    """Contrat minimal d'une source de barres OHLCV.

    Une source conforme expose uniquement get_bars — pas de connexion,
    pas de disconnect (géré en dehors si besoin).
    """

    def get_bars(
        self,
        symbol: str,
        lookback: str,
        interval: str,
    ) -> list[Bar]:
        """Retourne les barres OHLCV pour symbol.

        Args:
            symbol:   Ticker interne casys-trader (format yfinance).
            lookback: Période ('5d','1mo','3mo','6mo','1y').
            interval: Taille de barre ('15m','30m','1h','4h','1d').

        Returns:
            list[Bar] non vide.

        Raises:
            MarketError: code machine-readable, jamais d'exception brute.
        """
        ...


# ---------------------------------------------------------------------------
# YFinanceDataSource
# ---------------------------------------------------------------------------

class YFinanceDataSource:
    """Wrapper mince autour de market.get_bars (yfinance).

    Aucune logique propre : délègue intégralement à market.get_bars
    pour bénéficier de l'agrégation 4h existante.
    """

    def get_bars(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "1h",
    ) -> list[Bar]:
        from trader.tools import market
        return market.get_bars(symbol, lookback, interval)
```

- [ ] **Étape 1.4 : Vérifier vert**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_data_source.py::TestDataSourceProtocol -v 2>&1 | tail -10
```

Attendu : `4 passed`.

---

## Task 2 : `CompositeDataSource` — routing, fallback, `last_source`

**Files:**
- Modify: `trader/tools/data_source.py` (ajouter `CompositeDataSource`)
- Test: `tests/test_data_source.py` (ajouter `TestCompositeDataSource`)

### Contexte
- `trader/tools/market.py:69` — `assess_freshness(bars, *, now, max_age_minutes) -> Freshness`
- `trader/tools/market.py:34` — `class Freshness(fresh: bool, reason: str|None, age_minutes: float|None)`
- La garde stale : `freshness.fresh == False` → les barres sont périmées → fallback.
- Routing : `fnmatch.fnmatch(symbol, pattern)` — `*` matche tout, `*.TW` matche `2330.TW`.

- [ ] **Étape 2.1 : Écrire les tests rouges CompositeDataSource**

Ajouter à `tests/test_data_source.py` (après `TestDataSourceProtocol`) :

```python
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

    def test_route_exacte_utilise_premiere_source_disponible(self):
        """Symbole exact matché → première source de la route est utilisée."""
        from datetime import datetime, timezone
        from trader.tools.data_source import CompositeDataSource

        bar = self._bar()
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

        bar = self._bar()
        src = self._source([bar], name="tw")
        routes = [{"symbols": ["*.TW"], "sources": ["tw"]}]
        composite = CompositeDataSource(routes=routes, sources={"tw": src})

        result = composite.get_bars("2330.TW", "5d", "1h")
        assert result == [bar]
        assert composite.last_source("2330.TW") == "tw"

    def test_premiere_route_gagne_sur_catch_all(self):
        """Première route qui matche prend la priorité sur * en fin de liste."""
        from trader.tools.data_source import CompositeDataSource

        bar_a = self._bar("2026-06-10T10:00:00+00:00")
        bar_b = self._bar("2026-06-10T09:00:00+00:00")
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

        bar = self._bar()
        src_fail = self._source(None, name="fail")
        src_ok = self._source([bar], name="ok")

        routes = [{"symbols": ["*"], "sources": ["fail", "ok"]}]
        composite = CompositeDataSource(routes=routes, sources={"fail": src_fail, "ok": src_ok})

        result = composite.get_bars("SPY", "5d", "1h")
        assert result == [bar]
        assert composite.last_source("SPY") == "ok"

    def test_fallback_sur_stale(self, monkeypatch):
        """Si la première source retourne des barres stale → fallback sur suivante."""
        from datetime import datetime, timezone
        from trader.tools import market
        from trader.tools.data_source import CompositeDataSource

        bar_stale = self._bar("2024-01-01T00:00:00+00:00")
        bar_fresh = self._bar("2026-06-10T10:00:00+00:00")
        src_stale = self._source([bar_stale], name="stale")
        src_fresh = self._source([bar_fresh], name="fresh")

        # Monkeypatch assess_freshness : bar_stale → stale, bar_fresh → fresh
        original_assess = market.assess_freshness

        def fake_assess(bars, *, now, max_age_minutes):
            if bars and bars[0].ts.startswith("2024"):
                from trader.tools.market import Freshness
                return Freshness(False, "too_old", 9999.0)
            return original_assess(bars, now=now, max_age_minutes=max_age_minutes)

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

        bar = self._bar()
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
```

- [ ] **Étape 2.2 : Vérifier rouge**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_data_source.py::TestCompositeDataSource -v 2>&1 | head -20
```

Attendu : `ImportError` ou `AttributeError` sur `CompositeDataSource`.

- [ ] **Étape 2.3 : Implémenter `CompositeDataSource` dans `trader/tools/data_source.py`**

Ajouter APRÈS la classe `YFinanceDataSource` :

```python
# ---------------------------------------------------------------------------
# CompositeDataSource
# ---------------------------------------------------------------------------

class CompositeDataSource:
    """Route chaque symbole vers une liste ordonnée de sources.

    Config de routes (première qui matche gagne, fnmatch) :
        routes = [
            {"symbols": ["EURUSD=X", "USDJPY=X"], "sources": ["ib", "yfinance"]},
            {"symbols": ["*"],                      "sources": ["yfinance", "ib"]},
        ]
        sources = {"ib": IBDataSource(...), "yfinance": YFinanceDataSource()}

    Fallback déclenché si :
      - la source lève une exception (n'importe laquelle)
      - la source retourne des barres jugées stale par market.assess_freshness

    Source listée dans une route mais absente du dict sources → sautée
    (log machine-readable, pas d'exception).

    Toutes en échec → MarketError("all_sources_failed", ...).
    Symbole sans route → MarketError("no_route", ...).
    """

    # Budget de fraîcheur utilisé pour évaluer les barres avant fallback.
    # Valeur conservative (15 min de grâce) : si une source retourne des barres
    # plus vieilles que l'intervalle demandé + 15 min → stale → fallback.
    _FRESHNESS_GRACE_MINUTES = 15.0

    def __init__(
        self,
        *,
        routes: list[dict],
        sources: dict[str, object],
    ) -> None:
        """
        Args:
            routes:  Liste ordonnée de routes.
                     Chaque route : {"symbols": [str], "sources": [str]}.
            sources: Dict {nom: DataSource} des sources disponibles.
        """
        self._routes = routes
        self._sources = sources
        self._last_source: dict[str, str] = {}

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "1h",
    ) -> list[Bar]:
        """Retourne les barres pour symbol en essayant les sources dans l'ordre.

        Raises:
            MarketError("no_route"):          aucune route ne matche symbol.
            MarketError("all_sources_failed"): toutes les sources ont échoué.
        """
        from datetime import datetime, timezone
        from trader.tools import market

        source_names = self._resolve_route(symbol)
        if source_names is None:
            raise MarketError("no_route", f"{symbol}: aucune route ne matche")

        max_age = market.freshness_budget_minutes(interval, grace_minutes=self._FRESHNESS_GRACE_MINUTES)
        now = datetime.now(timezone.utc)

        last_error: MarketError | None = None

        for name in source_names:
            source = self._sources.get(name)
            if source is None:
                log.info(
                    '{"event":"source_skipped","symbol":"%s","source":"%s",'
                    '"reason":"absent_du_dict"}',
                    symbol, name,
                )
                continue

            try:
                bars = source.get_bars(symbol, lookback, interval)
            except Exception as exc:  # noqa: BLE001
                last_error = (
                    exc if isinstance(exc, MarketError)
                    else MarketError("source_error", f"{symbol}@{name}: {exc}")
                )
                log.info(
                    '{"event":"source_fallback","symbol":"%s","source":"%s",'
                    '"reason":"exception","code":"%s"}',
                    symbol, name,
                    last_error.code,
                )
                continue

            # Garde fraîcheur : barres stale → fallback
            freshness = market.assess_freshness(bars, now=now, max_age_minutes=max_age)
            if not freshness.fresh:
                last_error = MarketError(
                    "stale_data",
                    f"{symbol}@{name}: reason={freshness.reason} age={freshness.age_minutes}min",
                )
                log.info(
                    '{"event":"source_fallback","symbol":"%s","source":"%s",'
                    '"reason":"stale","stale_reason":"%s"}',
                    symbol, name,
                    freshness.reason,
                )
                continue

            # Succès
            self._last_source[symbol] = name
            return bars

        # Toutes en échec
        if last_error is None:
            last_error = MarketError("all_sources_failed", f"{symbol}: toutes sources absentes ou sautées")
        raise MarketError(
            "all_sources_failed",
            f"{symbol}: toutes sources épuisées — dernier: {last_error.code}: {last_error.context}",
        )

    def last_source(self, symbol: str) -> str | None:
        """Retourne le nom de la source qui a servi symbol lors du dernier appel."""
        return self._last_source.get(symbol)

    # ------------------------------------------------------------------
    # Routing interne
    # ------------------------------------------------------------------

    def _resolve_route(self, symbol: str) -> list[str] | None:
        """Retourne la liste de sources de la première route qui matche symbol."""
        for route in self._routes:
            for pattern in route["symbols"]:
                if symbol == pattern or fnmatch.fnmatch(symbol, pattern):
                    return list(route["sources"])
        return None
```

- [ ] **Étape 2.4 : Vérifier vert**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_data_source.py::TestCompositeDataSource -v 2>&1 | tail -15
```

Attendu : `9 passed`.

---

## Task 3 : Config `config/data_sources.yaml` + parser fail-fast

**Files:**
- Create: `config/data_sources.yaml`
- Modify: `trader/tools/data_source.py` (ajouter `load_composite_from_config`)
- Test: `tests/test_data_source.py` (ajouter `TestDataSourceConfig`)

### Contexte
- L'exemple canonique de config est dans la spec §3.4.
- `_load_yaml` existe dans `trader/daemon.py:129` — ne pas dupliquer, utiliser `yaml.safe_load` directement dans `data_source.py`.
- Profils valides : `paper`, `prod`. Source valides dans la config : `ib`, `yfinance`.
- Fail-fast : profil inconnu, source inconnue dans route, YAML invalide → `MarketError` immédiat.

- [ ] **Étape 3.1 : Écrire les tests rouges Config**

Ajouter à `tests/test_data_source.py` :

```python
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
        import os
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
        """Source nommée dans route mais absente de available_sources → MarketError('unknown_source')."""
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
```

- [ ] **Étape 3.2 : Vérifier rouge**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_data_source.py::TestDataSourceConfig -v 2>&1 | head -20
```

Attendu : `ImportError` ou `AttributeError` sur `load_composite_from_config`.

- [ ] **Étape 3.3 : Implémenter `load_composite_from_config` dans `trader/tools/data_source.py`**

Ajouter à la fin de `trader/tools/data_source.py` :

```python
# ---------------------------------------------------------------------------
# Loader config YAML
# ---------------------------------------------------------------------------

from pathlib import Path


def load_composite_from_config(
    config_path: Path | str,
    *,
    available_sources: dict[str, object],
    profile_override: str | None = None,
) -> tuple["CompositeDataSource", str]:
    """Charge config/data_sources.yaml et retourne (CompositeDataSource, profil_utilisé).

    Args:
        config_path:       Chemin absolu ou relatif vers le YAML.
        available_sources: Dict {nom: DataSource} des sources construites au démarrage.
        profile_override:  Si fourni, force le profil (ignore `profile:` dans le YAML).

    Returns:
        (composite, profile_used) — tuple pour l'observabilité.

    Raises:
        MarketError("config_not_found"):  fichier absent.
        MarketError("invalid_config"):    YAML invalide ou structure manquante.
        MarketError("unknown_profile"):   profil demandé absent des profils déclarés.
        MarketError("unknown_source"):    source nommée dans route absente de available_sources.
    """
    import yaml as _yaml

    p = Path(config_path)

    # 1. Lecture fichier
    if not p.exists():
        raise MarketError("config_not_found", str(p))

    try:
        raw = _yaml.safe_load(p.read_text(encoding="utf-8"))
    except _yaml.YAMLError as exc:
        raise MarketError("invalid_config", f"{p}: {exc}") from exc

    if not isinstance(raw, dict):
        raise MarketError("invalid_config", f"{p}: racine non-dict")

    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        raise MarketError("invalid_config", f"{p}: clé 'profiles' manquante ou non-dict")

    # 2. Résolution du profil
    profile_name = profile_override or raw.get("profile")
    if not profile_name:
        raise MarketError("invalid_config", f"{p}: clé 'profile' manquante et aucun override")

    if profile_name not in profiles:
        raise MarketError(
            "unknown_profile",
            f"profil={profile_name!r} absent de {sorted(profiles)}",
        )

    profile_cfg = profiles[profile_name]
    routes_raw = profile_cfg.get("routes", [])
    if not isinstance(routes_raw, list):
        raise MarketError("invalid_config", f"{p}: profil {profile_name!r}: 'routes' non-list")

    # 3. Validation fail-fast : toutes les sources déclarées doivent être disponibles
    for route in routes_raw:
        for src_name in route.get("sources", []):
            if src_name not in available_sources:
                raise MarketError(
                    "unknown_source",
                    f"source={src_name!r} dans profil {profile_name!r} absente de available_sources={sorted(available_sources)}",
                )

    # 4. Construction
    composite = CompositeDataSource(routes=routes_raw, sources=available_sources)
    return composite, profile_name
```

- [ ] **Étape 3.4 : Créer `config/data_sources.yaml`**

```yaml
# config/data_sources.yaml — routage multi-sources casys-trader
#
# profile : profil actif par défaut (overridable par --data-profile / TRADER_DATA_PROFILE).
# profiles : table des profils. Chaque profil = liste ordonnée de routes.
#   route : première route dont un pattern matche le symbole gagne.
#   sources : liste ordonnée (primaire → fallbacks).
#
# Symboles : format yfinance (ex: EURUSD=X, SPY, 2330.TW).
# Patterns : exact ou glob fnmatch (*, *.TW).
# Sources disponibles : ib, yfinance.

profile: paper

profiles:
  paper:
    routes:
      # FX : IDEALPRO IB est temps réel gratuit → priorité IB sur yfinance (différé)
      - symbols: [EURUSD=X, USDJPY=X]
        sources: [ib, yfinance]
      # Tout le reste : yfinance (~10 min), fallback IB (~15 min différé)
      - symbols: ["*"]
        sources: [yfinance, ib]

  prod:
    routes:
      # Prod : IB uniquement (market_data_type=1 + souscriptions actives)
      - symbols: ["*"]
        sources: [ib]
```

- [ ] **Étape 3.5 : Vérifier vert**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_data_source.py::TestDataSourceConfig -v 2>&1 | tail -15
```

Attendu : `7 passed`.

---

## Task 4 : Câblage daemon — `--data-profile`, construction des sources, observabilité

**Files:**
- Modify: `trader/daemon.py`
- Create: `tests/test_daemon_data_sources.py`

### Contexte à lire d'abord
- `trader/daemon.py:1514` — `def main(argv)` : argparse, boucle principale.
- `trader/daemon.py:1575` — `data_source = None` puis `IBDataSource` construit si `is None`.
- `trader/daemon.py:1657` — `except market.MarketError` : IB down → cycle sauté.
- `trader/daemon.py:1164` — `entry = {"symbol": sym, ...}` — champ `data_source` à ajouter ici.
- `trader/daemon.py:50` — `ROOT = Path(__file__).resolve().parent.parent`.
- `trader/daemon.py:129` — `_load_yaml` helper existant.
- `tests/test_daemon_ib_runtime.py:44` — pattern complet d'un test daemon (monkeypatch ROOT, STATE_DIR, connect_ib, IBDataSource, run_cycle).

### Changements daemon

**Dans `main()`** (après les args existants) :
1. Ajouter `--data-profile` CLI + env `TRADER_DATA_PROFILE`.
2. Construire les sources disponibles : `yfinance` toujours ; `ib` si `connect_ib` réussit.
3. Si `config/data_sources.yaml` **existe** → `load_composite_from_config` → `CompositeDataSource`.
4. Si **absent** → `IBDataSource` direct (comportement actuel inchangé).
5. En profil paper avec IB down : log warning mais NE PAS sauter le cycle.

**Dans `run_cycle` / la boucle de décision** :
1. Ajouter `entry["data_source"] = composite.last_source(sym) or "unknown"` juste après la construction du `entry` dict (ligne 1164).

- [ ] **Étape 4.1 : Écrire les tests rouges daemon**

```python
# tests/test_daemon_data_sources.py
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
        """--data-profile prod → profil prod utilisé."""
        _write_runtime_config(tmp_path)
        _write_data_sources_config(tmp_path, profile="paper")
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        profiles_used: list[str] = []

        # Monkeypatch load_composite_from_config pour capturer le profile_override
        import trader.tools.data_source as ds_mod
        original_load = ds_mod.load_composite_from_config

        def fake_load(cfg_path, *, available_sources, profile_override=None):
            profiles_used.append(profile_override)
            return original_load(cfg_path, available_sources=available_sources, profile_override=profile_override)

        monkeypatch.setattr(ds_mod, "load_composite_from_config", fake_load)

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
        """TRADER_DATA_PROFILE=prod → profil prod utilisé."""
        _write_runtime_config(tmp_path)
        _write_data_sources_config(tmp_path, profile="paper")
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        monkeypatch.setenv("TRADER_DATA_PROFILE", "prod")
        profiles_used: list[str] = []

        import trader.tools.data_source as ds_mod
        original_load = ds_mod.load_composite_from_config

        def fake_load(cfg_path, *, available_sources, profile_override=None):
            profiles_used.append(profile_override)
            return original_load(cfg_path, available_sources=available_sources, profile_override=profile_override)

        monkeypatch.setattr(ds_mod, "load_composite_from_config", fake_load)

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


class TestDaemonDecisionDataSourceField:
    """Le champ data_source est enregistré dans chaque entrée de décision."""

    def test_data_source_present_dans_entry_decision(
        self, monkeypatch, tmp_path
    ):
        """run_cycle : entry de décision doit contenir 'data_source'."""
        from trader.tools.market import Bar
        from trader.tools.scheduler import Scheduler
        from trader.codex_client import Decision

        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"

        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        bar = Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0)

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
```

- [ ] **Étape 4.2 : Vérifier rouge**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_daemon_data_sources.py -v 2>&1 | head -30
```

Attendu : `ImportError` ou `AttributeError` ou `FAILED` (pas `CompositeDataSource`, pas `--data-profile`, pas `data_source` dans entry).

- [ ] **Étape 4.3 : Modifier `trader/daemon.py` — ajouter `--data-profile` et logique composite**

**Import à ajouter** en haut du fichier (après les imports existants) :

Chercher la ligne `from .tools.ib_source import IBDataSource, connect_ib` et ajouter juste après :

```python
from .tools.data_source import CompositeDataSource, YFinanceDataSource, load_composite_from_config
```

**Dans `main()`** — ajouter l'argument CLI après `--ib-client-id` (vers ligne 1565) :

```python
parser.add_argument(
    "--data-profile",
    default=os.getenv("TRADER_DATA_PROFILE"),
    help="profil de routing data (paper|prod). Override config/data_sources.yaml. Env: TRADER_DATA_PROFILE",
)
```

**Remplacer le bloc de construction de data_source** dans la boucle `while True` (lignes 1575-1586) :

Avant (comportement actuel) :
```python
    data_source = None
    try:
        while True:
            sleep_seconds: float | None = None
            stop_after_iteration = False
            try:
                if data_source is None:
                    ib = connect_ib(args.ib_host, args.ib_port, args.ib_client_id)
                    data_source = IBDataSource(
                        ib,
                        reconnect_factory=lambda: connect_ib(args.ib_host, args.ib_port, args.ib_client_id),
                    )
```

Après (nouveau comportement) :
```python
    data_source = None
    _data_sources_cfg = ROOT / "config" / "data_sources.yaml"
    _use_composite = _data_sources_cfg.exists()
    try:
        while True:
            sleep_seconds: float | None = None
            stop_after_iteration = False
            try:
                if data_source is None:
                    if _use_composite:
                        # Config présente : construire composite
                        # IB optionnel — si down en profil paper, on continue sans lui
                        _ib_source: IBDataSource | None = None
                        try:
                            _ib_obj = connect_ib(args.ib_host, args.ib_port, args.ib_client_id)
                            _ib_source = IBDataSource(
                                _ib_obj,
                                reconnect_factory=lambda: connect_ib(args.ib_host, args.ib_port, args.ib_client_id),
                            )
                        except market.MarketError as _ib_exc:
                            log.warning(
                                "IB indisponible, profil composite sans IB (%s): %s",
                                _ib_exc.code, _ib_exc.context,
                            )
                        available: dict[str, object] = {"yfinance": YFinanceDataSource()}
                        if _ib_source is not None:
                            available["ib"] = _ib_source
                        data_source, _profile_used = load_composite_from_config(
                            _data_sources_cfg,
                            available_sources=available,
                            profile_override=args.data_profile or None,
                        )
                        log.info("data_source=composite profil=%s sources=%s", _profile_used, sorted(available))
                    else:
                        # Comportement actuel inchangé (rétrocompat)
                        ib = connect_ib(args.ib_host, args.ib_port, args.ib_client_id)
                        data_source = IBDataSource(
                            ib,
                            reconnect_factory=lambda: connect_ib(args.ib_host, args.ib_port, args.ib_client_id),
                        )
```

**Ajuster le bloc `except market.MarketError`** (ligne 1657) pour ne logger que si NON composite :

Remplacer :
```python
            except market.MarketError as exc:
                if data_source is not None:
                    _disconnect_quietly(data_source)
                    data_source = None
                log.warning("IB indisponible, cycle sauté (%s): %s", exc.code, exc.context)
                _write_status(
                    "ib_connection_failed",
                    current_symbol=None,
                    error_code=exc.code,
                    error_context=exc.context,
                )
                if args.once:
                    stop_after_iteration = True
                else:
                    sleep_seconds = args.poll
```

Par :
```python
            except market.MarketError as exc:
                if data_source is not None:
                    _disconnect_quietly(data_source)
                    data_source = None
                if _use_composite:
                    # En mode composite la source est déjà construite — une
                    # MarketError ici vient du cycle lui-même (all_sources_failed).
                    # On logue mais on reset pour reconstruire au prochain tour.
                    log.warning("cycle échoué (%s): %s", exc.code, exc.context)
                else:
                    log.warning("IB indisponible, cycle sauté (%s): %s", exc.code, exc.context)
                _write_status(
                    "ib_connection_failed",
                    current_symbol=None,
                    error_code=exc.code,
                    error_context=exc.context,
                )
                if args.once:
                    stop_after_iteration = True
                else:
                    sleep_seconds = args.poll
```

- [ ] **Étape 4.4 : Ajouter `data_source` dans `entry` de décision**

Dans `run_cycle`, chercher la construction du `entry` dict (ligne ~1164) :

```python
        entry = {"symbol": sym, "action": decision.action, "qty": effective_quantity,
                 "confidence": decision.confidence, "rationale": decision.rationale,
                 ...
                 "indicator_watch_rejections": []}
```

Ajouter `"data_source"` au dict :

```python
        entry = {"symbol": sym, "action": decision.action, "qty": effective_quantity,
                 "confidence": decision.confidence, "rationale": decision.rationale,
                 "next_wake_in_minutes": next_wake_in_minutes,
                 "next_wake_requested": decision.next_wake_in_minutes,
                 "context_request": decision.context_request,
                 "intent": decision.intent,
                 "llm_provider": decision.llm_provider,
                 "llm_model": decision.llm_model,
                 "llm_fallback_reason": decision.llm_fallback_reason,
                 "llm_error": decision.llm_error,
                 "learning": decision.learning,
                 "trade_plan_created": False,
                 "indicator_watch_created": False,
                 "indicator_watch_requested": bool(decision.indicator_watch),
                 "indicator_watch_rejections": [],
                 "data_source": getattr(data_source, "last_source", lambda _: None)(sym)}
```

Note : `getattr(data_source, "last_source", lambda _: None)(sym)` fonctionne pour
`CompositeDataSource` (qui a `last_source`) et pour tout autre type de source (retourne `None`).

- [ ] **Étape 4.5 : Vérifier vert**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest tests/test_daemon_data_sources.py -v 2>&1 | tail -20
```

Attendu : `6 passed`.

---

## Task 5 : Suite complète verte + vérification non-régression

**Files:** (aucun fichier supplémentaire)

- [ ] **Étape 5.1 : Lancer la suite complète**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && uv run pytest -q 2>&1 | tail -5
```

Attendu : `490+ passed, 1 skipped` (476 existants + ~13 nouveaux).

- [ ] **Étape 5.2 : Si échecs hors périmètre — vérifier avec stash**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && git stash && uv run pytest -q --tb=no 2>&1 | tail -3
```

Si les mêmes tests échouent sans les changements → problème préexistant, ne pas corriger.

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && git stash pop
```

- [ ] **Étape 5.3 : Vérifier que les tests interdits n'ont pas été touchés**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && git diff --name-only | grep -E "consolidator|test_cli_semantic|test_daemon_learnings|test_llm|README"
```

Attendu : aucune ligne (fichiers protégés non modifiés).

---

## Auto-review spec

### Couverture spec

| Exigence spec | Tâche couverte |
|---|---|
| Protocol `DataSource` runtime_checkable | Task 1 |
| `YFinanceDataSource` wrapper `market.get_bars` | Task 1 |
| `IBDataSource` satisfait Protocol (non-régression) | Task 1 test 4 |
| Routing exact + glob fnmatch | Task 2 tests 1-3 |
| Première route gagne | Task 2 test 3 |
| Fallback sur exception | Task 2 test 4 |
| Fallback sur stale (`assess_freshness`) | Task 2 test 5 |
| Source manquante du dict → sautée | Task 2 test 6 |
| Toutes en échec → `MarketError("all_sources_failed")` | Task 2 test 7 |
| `last_source(symbol)` | Task 2 tests 1,4,5,6 |
| Config YAML profils paper/prod | Task 3 |
| Parsing fail-fast profil inconnu | Task 3 test 4,5 |
| Parsing fail-fast source inconnue | Task 3 test 6 |
| Parsing fail-fast YAML invalide | Task 3 test 7 |
| Config absente → comportement IB inchangé | Task 4 test 2 |
| `--data-profile` CLI | Task 4 test 3 |
| `TRADER_DATA_PROFILE` env | Task 4 test 4 |
| IB down + profil paper → cycle non-sauté | Task 4 test 5 |
| `data_source` dans entrée de décision | Task 4 `TestDaemonDecisionDataSourceField` |
| `config/data_sources.yaml` créé | Task 3 étape 3.4 |

### Cohérence des types

- `DataSource` Protocol : `get_bars(symbol, lookback, interval) -> list[Bar]` — cohérent Tasks 1-4.
- `CompositeDataSource.last_source(symbol) -> str | None` — cohérent Task 2 et Task 4.
- `load_composite_from_config(...) -> tuple[CompositeDataSource, str]` — cohérent Task 3 et Task 4.
- `entry["data_source"]` : `str | None` — cohérent avec `last_source` return type.

### Placeholders

Aucun TBD, TODO, ou "similar to" — tous les blocs de code sont complets.
