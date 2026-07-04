"""Tests for trader/rotation_wiring.py — pure helpers, no I/O."""

from __future__ import annotations

import pytest
import yaml

from trader.market.market_data import Bar
from trader.market.rotation.wiring import (
    venue_of,
    benchmark_ret_for,
    resolve_as_of,
    compute_gap_adverse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(close: float, ts: str = "2024-01-02", open_: float | None = None) -> Bar:
    """Minimal Bar with a known close (and ts)."""
    o = open_ if open_ is not None else close
    return Bar(ts=ts, open=o, high=close, low=close, close=close, volume=0.0)


def _bars_with_return(start: float, end: float, ts: str = "2024-01-05") -> list[Bar]:
    """Two bars giving a known window_return = end/start - 1."""
    return [
        _bar(start, ts="2024-01-01"),
        _bar(end, ts=ts),
    ]


# ---------------------------------------------------------------------------
# venue_of
# ---------------------------------------------------------------------------

class TestVenueOf:
    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("8299.TWO", "TW"),
            ("6488.TWO", "TW"),
            ("2330.TW", "TW"),
            ("SPY", "US"),
            ("HO.PA", "EU"),
            ("EURUSD=X", "FX"),
        ],
    )
    def test_contract_examples(self, symbol, expected):
        assert venue_of(symbol) == expected

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("ROG.SW", "EU"),
            ("AZN.L", "EU"),
            ("NOVO-B.CO", "EU"),
            ("SAN.MC", "EU"),
            ("ERIC-B.ST", "EU"),
            ("PROX.BR", "EU"),
            ("EDP.LS", "EU"),
            ("NOKIA.HE", "EU"),
            ("OMV.VI", "EU"),
            ("EQNR.OL", "EU"),
            ("SPY", "US"),
            ("AAPL", "US"),
            ("2330.TW", "TW"),
            ("8299.TWO", "TW"),
        ],
    )
    def test_extended_market_suffixes(self, symbol, expected):
        assert venue_of(symbol) == expected

    def test_spy_us(self):
        assert venue_of("SPY") == "US"

    def test_nvda_us(self):
        assert venue_of("NVDA") == "US"

    def test_ho_pa_eu(self):
        assert venue_of("HO.PA") == "EU"

    def test_rhm_de_eu(self):
        assert venue_of("RHM.DE") == "EU"

    def test_as_suffix_eu(self):
        assert venue_of("PHIA.AS") == "EU"

    def test_mi_suffix_eu(self):
        assert venue_of("ENI.MI") == "EU"

    def test_fchi_eu(self):
        assert venue_of("^FCHI") == "EU"

    def test_tw_suffix_tw(self):
        assert venue_of("2330.TW") == "TW"

    def test_fx_suffix_fx(self):
        assert venue_of("EURUSD=X") == "FX"

    def test_unknown_suffix_us(self):
        assert venue_of("ABC.ZZ") == "US"


# ---------------------------------------------------------------------------
# benchmark_ret_for
# ---------------------------------------------------------------------------

class TestBenchmarkRetFor:
    BENCHMARKS = {"US": "SPY", "EU": "^FCHI", "TW": "^TWII"}
    DEFAULT_BENCH = "SPY"

    def _call(self, bars_by_symbol, *, score_window=2):
        return benchmark_ret_for(
            bars_by_symbol,
            benchmarks=self.BENCHMARKS,
            default_benchmark=self.DEFAULT_BENCH,
            score_window=score_window,
        )

    def test_us_symbol_uses_spy_return(self):
        """SPY present → benchmark_ret for NVDA == SPY's window return."""
        spy_bars = _bars_with_return(100.0, 110.0)  # return = 0.10
        nvda_bars = _bars_with_return(200.0, 220.0)
        result = self._call({"NVDA": nvda_bars, "SPY": spy_bars})
        assert "NVDA" in result
        assert abs(result["NVDA"] - 0.10) < 1e-6

    def test_eu_symbol_uses_fchi(self):
        """^FCHI present → benchmark_ret for HO.PA == ^FCHI window return."""
        fchi_bars = _bars_with_return(7000.0, 7070.0)  # return = 0.01
        hopa_bars = _bars_with_return(50.0, 55.0)
        result = self._call({"HO.PA": hopa_bars, "^FCHI": fchi_bars})
        assert abs(result["HO.PA"] - 0.01) < 1e-6

    def test_missing_benchmark_gives_zero(self):
        """If the benchmark symbol is NOT in bars_by_symbol → 0.0."""
        # TW symbol but ^TWII is absent
        tw_bars = _bars_with_return(100.0, 105.0)
        result = self._call({"2330.TW": tw_bars})  # no ^TWII
        assert result["2330.TW"] == 0.0

    def test_benchmark_none_value_gives_zero(self):
        """compute_indicator_values returns None for return when <2 bars → 0.0."""
        spy_bars = [_bar(100.0)]  # only 1 bar → return = None
        nvda_bars = _bars_with_return(200.0, 220.0)
        result = self._call({"NVDA": nvda_bars, "SPY": spy_bars})
        assert result["NVDA"] == 0.0

    def test_fx_symbol_uses_default_benchmark_when_absent(self):
        """FX → bench = SPY via benchmarks; if SPY absent → 0.0."""
        fx_bars = _bars_with_return(1.0, 1.05)
        # SPY not in dict
        result = self._call({"EURUSD=X": fx_bars})
        assert result["EURUSD=X"] == 0.0

    def test_all_symbols_returned(self):
        """Result keys == bars_by_symbol keys."""
        spy_bars = _bars_with_return(100.0, 110.0)
        nvda_bars = _bars_with_return(200.0, 220.0)
        msft_bars = _bars_with_return(300.0, 330.0)
        result = self._call({"NVDA": nvda_bars, "MSFT": msft_bars, "SPY": spy_bars})
        assert set(result.keys()) == {"NVDA", "MSFT", "SPY"}


# ---------------------------------------------------------------------------
# resolve_as_of
# ---------------------------------------------------------------------------

class TestResolveAsOf:
    def test_empty_dict_returns_empty_string(self):
        assert resolve_as_of({}) == ""

    def test_single_symbol(self):
        bars = [_bar(100.0, ts="2024-03-01")]
        assert resolve_as_of({"SPY": bars}) == "2024-03-01"

    def test_returns_most_recent_last_bar_ts(self):
        bars_a = [_bar(100.0, ts="2024-01-01"), _bar(101.0, ts="2024-01-05")]
        bars_b = [_bar(200.0, ts="2024-01-01"), _bar(202.0, ts="2024-01-03")]
        result = resolve_as_of({"A": bars_a, "B": bars_b})
        assert result == "2024-01-05"

    def test_symbol_with_no_bars_is_ignored(self):
        bars_a = [_bar(100.0, ts="2024-02-10")]
        result = resolve_as_of({"SPY": bars_a, "EMPTY": []})
        assert result == "2024-02-10"

    def test_all_empty_bars_returns_empty_string(self):
        result = resolve_as_of({"SPY": [], "NVDA": []})
        assert result == ""

    def test_lexicographic_ts_comparison(self):
        """ISO 8601 dates sort lexicographically — validate the correct one wins."""
        bars_a = [_bar(1.0, ts="2024-12-31")]
        bars_b = [_bar(2.0, ts="2025-01-01")]
        assert resolve_as_of({"A": bars_a, "B": bars_b}) == "2025-01-01"


# ---------------------------------------------------------------------------
# compute_gap_adverse
# ---------------------------------------------------------------------------

def _ranked_item(symbol: str, bias: str) -> dict:
    return {"symbol": symbol, "directional_score": 1.0, "attractiveness": 1.0, "bias": bias}


def _two_bars(prev_close: float, last_open: float, last_close: float | None = None) -> list:
    """Deux barres : prev_close puis last_open (pour simuler le gap d'ouverture)."""
    lc = last_close if last_close is not None else last_open
    return [
        Bar(ts="2024-01-01", open=prev_close, high=prev_close, low=prev_close, close=prev_close, volume=0.0),
        Bar(ts="2024-01-02", open=last_open,  high=lc,         low=lc,         close=lc,         volume=0.0),
    ]


class TestComputeGapAdverse:
    GAP_THRESHOLD = 0.03

    def test_long_with_gap_down_5pct_is_adverse(self):
        """Long + gap d'ouverture -5% (seuil 3%) → adverse."""
        # prev_close=100, last_open=95 → gap = (95-100)/100 = -0.05
        bars = {"AAPL": _two_bars(prev_close=100.0, last_open=95.0)}
        ranked = [_ranked_item("AAPL", "long")]
        result = compute_gap_adverse(bars, ranked, gap_threshold=self.GAP_THRESHOLD)
        assert "AAPL" in result

    def test_short_with_gap_up_5pct_is_adverse(self):
        """Short + gap d'ouverture +5% (seuil 3%) → adverse."""
        # prev_close=100, last_open=105 → gap = (105-100)/100 = +0.05
        bars = {"TSLA": _two_bars(prev_close=100.0, last_open=105.0)}
        ranked = [_ranked_item("TSLA", "short")]
        result = compute_gap_adverse(bars, ranked, gap_threshold=self.GAP_THRESHOLD)
        assert "TSLA" in result

    def test_long_with_gap_up_5pct_is_not_adverse(self):
        """Long + gap d'ouverture +5% (hausse favorable) → PAS adverse."""
        # prev_close=100, last_open=105 → gap = +0.05 → favorable pour long
        bars = {"MSFT": _two_bars(prev_close=100.0, last_open=105.0)}
        ranked = [_ranked_item("MSFT", "long")]
        result = compute_gap_adverse(bars, ranked, gap_threshold=self.GAP_THRESHOLD)
        assert "MSFT" not in result

    def test_symbol_with_one_bar_not_adverse(self):
        """Symbole avec < 2 barres → ignoré, pas adverse."""
        bars = {"NVDA": [Bar(ts="2024-01-01", open=100.0, high=100.0, low=100.0, close=100.0, volume=0.0)]}
        ranked = [_ranked_item("NVDA", "long")]
        result = compute_gap_adverse(bars, ranked, gap_threshold=self.GAP_THRESHOLD)
        assert "NVDA" not in result

    def test_gap_exactly_at_threshold_not_adverse(self):
        """Gap exactement au seuil (non strict) → PAS adverse (condition stricte <=/->=)."""
        # Pour long : adverse si gap <= -threshold → gap = -0.03 → adverse
        # Pour short : adverse si gap >= threshold → gap = +0.03 → adverse
        bars_long = {"LONG": _two_bars(prev_close=100.0, last_open=97.0)}   # gap = -0.03
        ranked_long = [_ranked_item("LONG", "long")]
        result_long = compute_gap_adverse(bars_long, ranked_long, gap_threshold=self.GAP_THRESHOLD)
        assert "LONG" in result_long  # -0.03 <= -0.03 → adverse

    def test_returns_frozenset(self):
        """Le retour est bien un frozenset."""
        result = compute_gap_adverse({}, [], gap_threshold=self.GAP_THRESHOLD)
        assert isinstance(result, frozenset)

    def test_symbol_absent_from_bars_not_adverse(self):
        """Symbole dans ranked mais absent de bars_by_symbol → pas adverse."""
        ranked = [_ranked_item("XYZ", "long")]
        result = compute_gap_adverse({}, ranked, gap_threshold=self.GAP_THRESHOLD)
        assert "XYZ" not in result

    def test_prev_close_zero_ignored(self):
        """Si prev.close == 0, le symbole est ignoré (pas de division par 0)."""
        bars = {"ABC": _two_bars(prev_close=0.0, last_open=10.0)}
        ranked = [_ranked_item("ABC", "long")]
        result = compute_gap_adverse(bars, ranked, gap_threshold=self.GAP_THRESHOLD)
        assert "ABC" not in result


# ---------------------------------------------------------------------------
# build_rank_fn — gap_adverse key present
# ---------------------------------------------------------------------------

class TestBuildRankFnGapAdverse:
    def test_rank_fn_returns_gap_adverse_key(self, tmp_path):
        """rank_fn() retourne un dict avec la clé 'gap_adverse' (frozenset)."""
        import yaml

        # Créer les fichiers de config minimaux
        (tmp_path / "pool.yaml").write_text(
            yaml.dump({"symbols": ["AAPL", "MSFT"]}),
            encoding="utf-8",
        )
        (tmp_path / "radar.yaml").write_text(
            "gap_threshold: 0.03\n",
            encoding="utf-8",
        )
        (tmp_path / "conviction.yaml").write_text("{}", encoding="utf-8")

        # fetch_fn fake : retourne 2 barres par symbole
        def fake_fetch(symbols: list[str]) -> dict:
            result = {}
            for s in symbols:
                result[s] = [
                    Bar(ts="2024-01-01", open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0),
                    Bar(ts="2024-01-02", open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0),
                ]
            return result

        from trader.market.rotation.wiring import build_rank_fn

        rank_fn = build_rank_fn(str(tmp_path), fetch_fn=fake_fetch, as_of="2024-01-02")
        result = rank_fn()

        assert "gap_adverse" in result
        assert isinstance(result["gap_adverse"], frozenset)


# ---------------------------------------------------------------------------
# build_llm_override_fn
# ---------------------------------------------------------------------------

class TestBuildLlmOverrideFn:
    """Tests pour build_llm_override_fn — factory câblant router LLM réel."""

    def test_returns_callable(self, monkeypatch):
        """build_llm_override_fn retourne un callable."""
        from trader.agent import llm as llm_module

        class _FakeCompletion:
            text = '{"add":[],"remove":[]}'

        class _FakeRouter:
            def complete(self, prompt, *, timeout_s):
                return _FakeCompletion()

        monkeypatch.setattr(llm_module, "build_default_router_from_env", lambda **kw: _FakeRouter())

        from trader.market.rotation.wiring import build_llm_override_fn

        fn = build_llm_override_fn()
        assert callable(fn)

    def test_override_fn_parses_llm_response(self, monkeypatch):
        """Avec un router fake renvoyant du JSON, override_fn produit le bon dict."""
        from trader.agent import llm as llm_module

        _JSON = '{"add":[],"remove":[]}'

        class _FakeCompletion:
            text = _JSON

        class _FakeRouter:
            def complete(self, prompt, *, timeout_s):
                return _FakeCompletion()

        monkeypatch.setattr(llm_module, "build_default_router_from_env", lambda **kw: _FakeRouter())

        from trader.market.rotation.wiring import build_llm_override_fn

        fn = build_llm_override_fn()
        result = fn({"ranked": [], "default_hot": []})
        assert result == {"add": [], "remove": []}


# ---------------------------------------------------------------------------
# run_cli — câblage override_enabled
# ---------------------------------------------------------------------------

def _make_config_for_override(tmp_path: object, *, override_enabled: bool) -> object:
    """Crée un config_dir minimal avec override_enabled paramétrable."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    (config_dir / "pool.yaml").write_text(
        "symbols: [AAA, BBB]\nhard_exclusions: []\n",
        encoding="utf-8",
    )
    radar_cfg = {
        "atr_floor": 0.0,
        "min_coverage": 0.0,
        "amplitude_cap": 0.05,
        "cap_m": 5,
        "benchmarks": {"US": "SPY"},
        "default_benchmark": "SPY",
        "score_window_bars": 15,
        "w_trend": 1.0,
        "w_rs": 1.0,
        "w_amp": 1.0,
        "delta": 0.01,
        "dwell_days": 1,
        "emergency_score": 0.0,
        "override_enabled": override_enabled,
    }
    (config_dir / "radar.yaml").write_text(yaml.safe_dump(radar_cfg), encoding="utf-8")
    (config_dir / "conviction.yaml").write_text("{}\n", encoding="utf-8")
    return config_dir


def _make_bars(n: int = 20, *, base: float = 100.0) -> list[Bar]:
    bars = []
    for i in range(n):
        close = base * (1 + 0.005 * i)
        bars.append(Bar(
            ts=f"2026-05-{i + 1:02d}" if i < 27 else f"2026-06-{i - 26:02d}",
            open=close * 0.999,
            high=close * 1.002,
            low=close * 0.998,
            close=close,
            volume=1_000_000.0,
        ))
    return bars


def _fake_fetch(symbols: list[str]) -> dict[str, list[Bar]]:
    return {s: _make_bars(20, base=100.0 + hash(s) % 50) for s in symbols}


class TestRunCliOverrideWiring:
    """Tests du câblage override_fn dans run_cli selon override_enabled."""

    def test_override_disabled_uses_default_fn_not_router(self, tmp_path, monkeypatch):
        """override_enabled=false + override_fn=None → default_override_fn, router JAMAIS construit."""
        from trader.agent import llm as llm_module

        def _must_not_be_called(**kw):
            raise AssertionError("build_default_router_from_env NE DOIT PAS être appelé")

        monkeypatch.setattr(llm_module, "build_default_router_from_env", _must_not_be_called)

        from trader.market.rotation.wiring import run_cli

        config_dir = _make_config_for_override(tmp_path, override_enabled=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Ne doit pas lever — le router ne doit pas être construit
        result = run_cli(
            config_dir,
            state_dir,
            fetch_fn=_fake_fetch,
            sticky_fn=lambda: set(),
            as_of="2026-06-15",
        )
        assert result["written"] is True

    def test_override_enabled_builds_router(self, tmp_path, monkeypatch):
        """override_enabled=true + override_fn=None → build_default_router_from_env est appelé."""
        from trader.agent import llm as llm_module

        _JSON = '{"add":[],"remove":[]}'

        class _FakeCompletion:
            text = _JSON

        class _FakeRouter:
            def complete(self, prompt, *, timeout_s):
                return _FakeCompletion()

        router_calls = []

        def _fake_build(**kw):
            router_calls.append(kw)
            return _FakeRouter()

        monkeypatch.setattr(llm_module, "build_default_router_from_env", _fake_build)

        from trader.market.rotation.wiring import run_cli

        config_dir = _make_config_for_override(tmp_path, override_enabled=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = run_cli(
            config_dir,
            state_dir,
            fetch_fn=_fake_fetch,
            sticky_fn=lambda: set(),
            as_of="2026-06-15",
        )
        assert result["written"] is True
        assert len(router_calls) >= 1, "build_default_router_from_env doit avoir été appelé"
