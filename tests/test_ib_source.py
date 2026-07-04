"""Tests TDD du connecteur IBDataSource.

Toutes les suites ici tournent SANS réseau — le client ib est injecté
comme un FakeIB qui renvoie des barres synthétiques.

Convention ib_async (vérifiée sur 2.1.0) :
  - formatDate=2 intraday : bar.date = datetime aware UTC
    (chemin: s.isdigit() → datetime.fromtimestamp(int(s), utc))
  - formatDate=2 daily    : bar.date = datetime.date (naïf)
  - qualifyContracts()    : retourne la liste des contrats qualifiés ([] si échec)
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

import pytest

from trader.market.market_data import Bar, MarketError


# ---------------------------------------------------------------------------
# Fake ib client (zéro réseau)
# ---------------------------------------------------------------------------

class _FakeBarData:
    """Simule un ib_async.BarData avec bar.date = datetime aware UTC (intraday)."""

    def __init__(
        self,
        ts: datetime,
        open: float = 100.0,
        high: float = 101.0,
        low: float = 99.0,
        close: float = 100.5,
        volume: float = 1000.0,
    ) -> None:
        self.date = ts
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _make_bar(offset_secs: int = 0) -> _FakeBarData:
    ts = datetime(2026, 6, 5, 14, 0, offset_secs, tzinfo=timezone.utc)
    return _FakeBarData(ts, open=100.0, high=102.0, low=98.0, close=101.0, volume=500.0)


class FakeIB:
    """Stub minimal du client ib_async.IB.

    Configuré par les tests via les attributs :
        - qualify_result    : list retourné par qualifyContracts ([] = échec, None = succès)
        - bars_result       : list[_FakeBarData] retourné par reqHistoricalData
        - raise_on_hist     : si True, reqHistoricalData lève une RuntimeError
        - raise_on_qualify  : si True, qualifyContracts lève une Exception
        - raise_on_mdt      : si True, reqMarketDataType lève une Exception
        - calls             : enregistre les appels pour assertions
    """

    def __init__(
        self,
        *,
        qualify_result: list | None = None,
        bars_result: list | None = None,
        raise_on_hist: bool = False,
        raise_on_qualify: bool = False,
        raise_on_mdt: bool = False,
    ) -> None:
        self._qualify_result = qualify_result  # None = succès (retourne le contrat inchangé)
        self._bars_result = bars_result if bars_result is not None else [_make_bar()]
        self._raise_on_hist = raise_on_hist
        self._raise_on_qualify = raise_on_qualify
        self._raise_on_mdt = raise_on_mdt
        self.calls: dict[str, list] = {"qualifyContracts": [], "reqHistoricalData": []}

    def reqMarketDataType(self, _type: int) -> None:
        if self._raise_on_mdt:
            raise RuntimeError("socket cassée post-connect")

    def qualifyContracts(self, *contracts: Any) -> list:
        self.calls["qualifyContracts"].append(contracts)
        if self._raise_on_qualify:
            raise RuntimeError("RequestError: qualify a explosé")
        if self._qualify_result is not None:
            return self._qualify_result
        # Succès par défaut : retourne les contrats avec conId=1 pour simuler
        # un contrat qualifié (conId=0 = non qualifié dans ib_async réel).
        for c in contracts:
            if hasattr(c, "conId"):
                c.conId = 1
        return list(contracts)

    def reqHistoricalData(
        self,
        contract: Any,
        endDateTime: Any,
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRTH: bool,
        formatDate: int = 2,
        **kwargs: Any,
    ) -> list:
        self.calls["reqHistoricalData"].append({
            "contract": contract,
            "durationStr": durationStr,
            "barSizeSetting": barSizeSetting,
            "whatToShow": whatToShow,
            "useRTH": useRTH,
            "formatDate": formatDate,
        })
        if self._raise_on_hist:
            raise RuntimeError("connexion perdue")
        return self._bars_result


# ---------------------------------------------------------------------------
# Import tardif (après écriture du module)
# ---------------------------------------------------------------------------

def _get_source():
    from trader.market.ib_source import IBDataSource
    return IBDataSource


# ---------------------------------------------------------------------------
# Tests mapping symbole → Contract (sec_type)
# ---------------------------------------------------------------------------

class TestContractBuilding:
    """Un test par sec_type du YAML."""

    def _ds(self, fake_ib: FakeIB | None = None) -> Any:
        IBDataSource = _get_source()
        ib = fake_ib or FakeIB()
        return IBDataSource(ib)

    def test_stk_construit_stock(self) -> None:
        """SPY → Stock(symbol='SPY', exchange='SMART', currency='USD')."""
        from ib_async import Stock
        ds = self._ds()
        contract = ds._build_contract("SPY")
        assert isinstance(contract, Stock)
        assert contract.symbol == "SPY"
        assert contract.exchange == "SMART"
        assert contract.currency == "USD"

    def test_cash_construit_forex_avec_pair(self) -> None:
        """EURUSD=X → Forex(pair='EURUSD', exchange='IDEALPRO').

        ib_async Forex stocke la paire via symbol+currency (pas un champ .pair direct).
        Forex(pair='EURUSD') → symbol='EUR', currency='USD'.
        """
        from ib_async import Forex
        ds = self._ds()
        contract = ds._build_contract("EURUSD=X")
        assert isinstance(contract, Forex)
        # Forex décompose pair → symbol (base) + currency (quote)
        assert contract.symbol == "EUR"
        assert contract.currency == "USD"
        assert contract.exchange == "IDEALPRO"

    def test_ind_construit_index(self) -> None:
        """^FCHI → Index(symbol='CAC40', exchange='MONEP', currency='EUR')."""
        from ib_async import Index
        ds = self._ds()
        contract = ds._build_contract("^FCHI")
        assert isinstance(contract, Index)
        assert contract.symbol == "CAC40"
        assert contract.exchange == "MONEP"
        assert contract.currency == "EUR"

    def test_contfut_construit_contfuture(self) -> None:
        """CL=F → ContFuture(symbol='CL', exchange='NYMEX', currency='USD')."""
        from ib_async import ContFuture
        ds = self._ds()
        contract = ds._build_contract("CL=F")
        assert isinstance(contract, ContFuture)
        assert contract.symbol == "CL"
        assert contract.exchange == "NYMEX"
        assert contract.currency == "USD"

    def test_crypto_construit_crypto(self) -> None:
        """BTC-USD → Crypto(symbol='BTC', exchange='PAXOS', currency='USD')."""
        from ib_async import Crypto
        ds = self._ds()
        contract = ds._build_contract("BTC-USD")
        assert isinstance(contract, Crypto)
        assert contract.symbol == "BTC"
        assert contract.exchange == "PAXOS"
        assert contract.currency == "USD"

    def test_symbole_absent_du_mapping_leve_market_error(self) -> None:
        ds = self._ds()
        with pytest.raises(MarketError) as exc:
            ds._build_contract("INEXISTANT")
        assert exc.value.code == "unmapped_symbol"
        assert "INEXISTANT" in exc.value.context


# ---------------------------------------------------------------------------
# Tests mapping interval/lookback → chaînes IB
# ---------------------------------------------------------------------------

class TestUnitMapping:
    """Tables INTERVAL_MAP et LOOKBACK_MAP exposées comme constantes."""

    def test_interval_15m_mappe_sur_15_mins(self) -> None:
        from trader.market.ib_source import INTERVAL_MAP
        assert INTERVAL_MAP["15m"] == "15 mins"

    def test_interval_30m_mappe_sur_30_mins(self) -> None:
        from trader.market.ib_source import INTERVAL_MAP
        assert INTERVAL_MAP["30m"] == "30 mins"

    def test_interval_1h_mappe_sur_1_hour(self) -> None:
        from trader.market.ib_source import INTERVAL_MAP
        assert INTERVAL_MAP["1h"] == "1 hour"

    def test_interval_4h_mappe_sur_4_hours(self) -> None:
        from trader.market.ib_source import INTERVAL_MAP
        assert INTERVAL_MAP["4h"] == "4 hours"

    def test_interval_1d_mappe_sur_1_day(self) -> None:
        from trader.market.ib_source import INTERVAL_MAP
        assert INTERVAL_MAP["1d"] == "1 day"

    def test_lookback_5d_mappe_sur_5_D(self) -> None:
        from trader.market.ib_source import LOOKBACK_MAP
        assert LOOKBACK_MAP["5d"] == "5 D"

    def test_lookback_1mo_mappe_sur_1_M(self) -> None:
        from trader.market.ib_source import LOOKBACK_MAP
        assert LOOKBACK_MAP["1mo"] == "1 M"

    def test_lookback_3mo_mappe_sur_3_M(self) -> None:
        from trader.market.ib_source import LOOKBACK_MAP
        assert LOOKBACK_MAP["3mo"] == "3 M"

    def test_lookback_6mo_mappe_sur_6_M(self) -> None:
        from trader.market.ib_source import LOOKBACK_MAP
        assert LOOKBACK_MAP["6mo"] == "6 M"

    def test_lookback_1y_mappe_sur_1_Y(self) -> None:
        from trader.market.ib_source import LOOKBACK_MAP
        assert LOOKBACK_MAP["1y"] == "1 Y"

    def test_interval_invalide_leve_market_error(self) -> None:
        IBDataSource = _get_source()
        ds = IBDataSource(FakeIB())
        with pytest.raises(MarketError) as exc:
            ds.get_bars("SPY", lookback="5d", interval="2m")
        assert exc.value.code == "unsupported_interval"
        assert "2m" in exc.value.context

    def test_lookback_invalide_leve_market_error(self) -> None:
        IBDataSource = _get_source()
        ds = IBDataSource(FakeIB())
        with pytest.raises(MarketError) as exc:
            ds.get_bars("SPY", lookback="10d", interval="15m")
        assert exc.value.code == "unsupported_lookback"
        assert "10d" in exc.value.context


# ---------------------------------------------------------------------------
# Tests whatToShow par sec_type
# ---------------------------------------------------------------------------

class TestWhatToShow:
    """whatToShow dépend du sec_type (TRADES pour STK/CONTFUT, MIDPOINT pour CASH)."""

    def _call(self, symbol: str) -> dict:
        IBDataSource = _get_source()
        ib = FakeIB(bars_result=[_make_bar()])
        IBDataSource(ib).get_bars(symbol, lookback="5d", interval="15m")
        return ib.calls["reqHistoricalData"][-1]

    def test_stk_utilise_trades(self) -> None:
        call = self._call("SPY")
        assert call["whatToShow"] == "TRADES"

    def test_cash_utilise_midpoint(self) -> None:
        call = self._call("EURUSD=X")
        assert call["whatToShow"] == "MIDPOINT"

    def test_contfut_utilise_trades(self) -> None:
        call = self._call("CL=F")
        assert call["whatToShow"] == "TRADES"

    def test_ind_utilise_trades(self) -> None:
        call = self._call("^FCHI")
        assert call["whatToShow"] == "TRADES"

    def test_crypto_utilise_aggtrades(self) -> None:
        call = self._call("BTC-USD")
        assert call["whatToShow"] == "AGGTRADES"


# ---------------------------------------------------------------------------
# Tests conversion BarData → Bar
# ---------------------------------------------------------------------------

class TestBarConversion:
    """Vérification de la conversion IB BarData → Bar (ts UTC, OHLCV float)."""

    def _get_bars(self, bar_data_list: list) -> list[Bar]:
        IBDataSource = _get_source()
        ib = FakeIB(bars_result=bar_data_list)
        return IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")

    def test_ts_intraday_est_iso8601_utc(self) -> None:
        """Un datetime UTC aware devient '2026-06-05T14:00:00+00:00'."""
        ts = datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc)
        bars = self._get_bars([_FakeBarData(ts, open=100.0, high=102.0, low=98.0, close=101.0, volume=500.0)])
        assert len(bars) == 1
        assert bars[0].ts == "2026-06-05T14:00:00+00:00"

    def test_ts_daily_date_objet_est_converti_utc(self) -> None:
        """Un datetime.date donne '2026-06-05T00:00:00+00:00'."""
        fake = _FakeBarData.__new__(_FakeBarData)
        fake.date = date(2026, 6, 5)
        fake.open = 100.0
        fake.high = 102.0
        fake.low = 98.0
        fake.close = 101.0
        fake.volume = 1000.0
        bars = self._get_bars([fake])
        assert bars[0].ts == "2026-06-05T00:00:00+00:00"

    def test_ohlcv_sont_des_float(self) -> None:
        ts = datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc)
        bars = self._get_bars([_FakeBarData(ts, open=100, high=102, low=98, close=101, volume=500)])
        b = bars[0]
        assert isinstance(b.open, float)
        assert isinstance(b.high, float)
        assert isinstance(b.low, float)
        assert isinstance(b.close, float)
        assert isinstance(b.volume, float)

    def test_valeurs_ohlcv_sont_correctes(self) -> None:
        ts = datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc)
        bars = self._get_bars([_FakeBarData(ts, open=100.1, high=102.5, low=98.3, close=101.7, volume=12345.0)])
        b = bars[0]
        assert b.open == 100.1
        assert b.high == 102.5
        assert b.low == 98.3
        assert b.close == 101.7
        assert b.volume == 12345.0

    def test_plusieurs_barres_preservent_ordre(self) -> None:
        bds = [
            _FakeBarData(datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc), close=100.0),
            _FakeBarData(datetime(2026, 6, 5, 14, 15, 0, tzinfo=timezone.utc), close=101.0),
            _FakeBarData(datetime(2026, 6, 5, 14, 30, 0, tzinfo=timezone.utc), close=102.0),
        ]
        bars = self._get_bars(bds)
        assert len(bars) == 3
        assert bars[0].ts == "2026-06-05T14:00:00+00:00"
        assert bars[1].ts == "2026-06-05T14:15:00+00:00"
        assert bars[2].ts == "2026-06-05T14:30:00+00:00"

    def test_assess_freshness_parse_le_ts_correctement(self) -> None:
        """Vérification que le ts produit est parseable par assess_freshness."""
        from trader.market.market_data import assess_freshness
        ts = datetime(2026, 6, 5, 14, 0, 0, tzinfo=timezone.utc)
        IBDataSource = _get_source()
        ib = FakeIB(bars_result=[_FakeBarData(ts)])
        bars = IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")
        now = datetime(2026, 6, 5, 14, 10, 0, tzinfo=timezone.utc)
        f = assess_freshness(bars, now=now, max_age_minutes=30)
        assert f.fresh is True


# ---------------------------------------------------------------------------
# Tests fail-safe (MarketError machine-readable)
# ---------------------------------------------------------------------------

class TestFailSafe:
    """Erreurs IB → MarketError typée, jamais exception brute."""

    def test_serie_vide_leve_ib_no_data(self) -> None:
        IBDataSource = _get_source()
        ib = FakeIB(bars_result=[])
        with pytest.raises(MarketError) as exc:
            IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")
        assert exc.value.code == "ib_no_data"

    def test_qualify_failed_leve_ib_qualify_failed(self) -> None:
        """qualifyContracts retourne [] → ib_qualify_failed."""
        IBDataSource = _get_source()
        ib = FakeIB(qualify_result=[])
        with pytest.raises(MarketError) as exc:
            IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")
        assert exc.value.code == "ib_qualify_failed"
        assert "SPY" in exc.value.context

    def test_exception_client_est_capturee_en_market_error(self) -> None:
        """Exception brute du client → MarketError, pas de crash."""
        IBDataSource = _get_source()
        ib = FakeIB(raise_on_hist=True)
        with pytest.raises(MarketError) as exc:
            IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")
        assert exc.value.code == "ib_fetch_failed"
        # Jamais une RuntimeError brute
        assert isinstance(exc.value, MarketError)

    def test_socket_morte_retente_une_reconnexion_injectee_avant_abandon(self) -> None:
        IBDataSource = _get_source()
        first = FakeIB(raise_on_hist=True)
        second = FakeIB(bars_result=[_FakeBarData(datetime(2026, 6, 5, 14, 1, tzinfo=timezone.utc), close=103.0)])

        ds = IBDataSource(first, reconnect_factory=lambda: second)
        bars = ds.get_bars("SPY", lookback="5d", interval="15m")

        assert bars[0].close == 103.0
        assert len(first.calls["reqHistoricalData"]) == 1
        assert len(second.calls["reqHistoricalData"]) == 1

    def test_unmapped_symbol_leve_market_error(self) -> None:
        IBDataSource = _get_source()
        ib = FakeIB()
        with pytest.raises(MarketError) as exc:
            IBDataSource(ib).get_bars("NOPE_XYZ", lookback="5d", interval="15m")
        assert exc.value.code == "unmapped_symbol"

    def test_market_error_a_code_et_context(self) -> None:
        """MarketError respecte l'interface market.py : .code et .context."""
        IBDataSource = _get_source()
        ib = FakeIB(bars_result=[])
        exc_info = None
        try:
            IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")
        except MarketError as e:
            exc_info = e
        assert exc_info is not None
        assert hasattr(exc_info, "code")
        assert hasattr(exc_info, "context")
        assert isinstance(exc_info.code, str)
        assert isinstance(exc_info.context, str)


# ---------------------------------------------------------------------------
# Tests appels IB corrects (barSizeSetting + durationStr)
# ---------------------------------------------------------------------------

class TestIBCallParameters:
    """Vérifie que les paramètres passés à reqHistoricalData sont corrects."""

    def _call(self, *, symbol: str = "SPY", lookback: str = "5d", interval: str = "15m") -> dict:
        IBDataSource = _get_source()
        ib = FakeIB(bars_result=[_make_bar()])
        IBDataSource(ib).get_bars(symbol, lookback=lookback, interval=interval)
        return ib.calls["reqHistoricalData"][-1]

    def test_interval_15m_produit_barSizeSetting_correct(self) -> None:
        call = self._call(interval="15m")
        assert call["barSizeSetting"] == "15 mins"

    def test_interval_1h_produit_barSizeSetting_correct(self) -> None:
        call = self._call(interval="1h")
        assert call["barSizeSetting"] == "1 hour"

    def test_interval_4h_produit_barSizeSetting_correct(self) -> None:
        call = self._call(interval="4h")
        assert call["barSizeSetting"] == "4 hours"

    def test_interval_1d_produit_barSizeSetting_correct(self) -> None:
        call = self._call(interval="1d")
        assert call["barSizeSetting"] == "1 day"

    def test_lookback_5d_produit_durationStr_correct(self) -> None:
        call = self._call(lookback="5d")
        assert call["durationStr"] == "5 D"

    def test_lookback_1mo_produit_durationStr_correct(self) -> None:
        call = self._call(lookback="1mo")
        assert call["durationStr"] == "1 M"

    def test_lookback_1y_produit_durationStr_correct(self) -> None:
        call = self._call(lookback="1y")
        assert call["durationStr"] == "1 Y"

    def test_formatDate_est_2(self) -> None:
        call = self._call()
        assert call["formatDate"] == 2

    def test_useRTH_est_False(self) -> None:
        """useRTH=False pour capturer les barres hors heures régulières."""
        call = self._call()
        assert call["useRTH"] is False


# ---------------------------------------------------------------------------
# Finding 1 : qualifyContracts peut retourner [None] ou [contrat sans conId]
# ---------------------------------------------------------------------------

class TestQualifyPartialFailure:
    """Finding 1 : contrat partiellement non résolu doit lever ib_qualify_failed."""

    def test_qualify_retourne_none_leve_ib_qualify_failed(self) -> None:
        """qualifyContracts([None]) → ib_qualify_failed, pas de passage vers reqHistoricalData."""
        IBDataSource = _get_source()
        ib = FakeIB(qualify_result=[None])
        with pytest.raises(MarketError) as exc:
            IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")
        assert exc.value.code == "ib_qualify_failed"
        assert "SPY" in exc.value.context
        # reqHistoricalData ne doit PAS avoir été appelé
        assert ib.calls["reqHistoricalData"] == []

    def test_qualify_retourne_contrat_sans_conId_leve_ib_qualify_failed(self) -> None:
        """qualifyContracts([contrat avec conId=0]) → ib_qualify_failed."""

        class _UnresolvedContract:
            conId = 0  # conId 0 = non qualifié dans IB

        IBDataSource = _get_source()
        ib = FakeIB(qualify_result=[_UnresolvedContract()])
        with pytest.raises(MarketError) as exc:
            IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")
        assert exc.value.code == "ib_qualify_failed"
        assert ib.calls["reqHistoricalData"] == []


# ---------------------------------------------------------------------------
# Finding 2 : qualifyContracts non protégé → exception brute
# ---------------------------------------------------------------------------

class TestQualifyException:
    """Finding 2 : exception dans qualifyContracts → MarketError, jamais brute."""

    def test_exception_qualify_est_capturee_en_market_error(self) -> None:
        """RuntimeError dans qualifyContracts → MarketError (pas de crash)."""
        IBDataSource = _get_source()
        ib = FakeIB(raise_on_qualify=True)
        with pytest.raises(MarketError) as exc:
            IBDataSource(ib).get_bars("SPY", lookback="5d", interval="15m")
        assert exc.value.code == "ib_qualify_failed"
        assert isinstance(exc.value, MarketError)
        # reqHistoricalData ne doit PAS avoir été appelé
        assert ib.calls["reqHistoricalData"] == []


# ---------------------------------------------------------------------------
# Finding 3 : connect_ib — reqMarketDataType non protégé
# ---------------------------------------------------------------------------

class TestConnectIBFailSafe:
    """Finding 3 : exception dans reqMarketDataType après connect → MarketError."""

    def test_connect_retry_avec_backoff_avant_succes(self, monkeypatch) -> None:
        from trader.market import ib_source

        created = []
        sleeps: list[float] = []

        class _FakeIBConn:
            def __init__(self, *, should_fail: bool) -> None:
                self.should_fail = should_fail
                self.disconnects = 0

            def connect(self, *_a, **_kw) -> None:
                if self.should_fail:
                    raise RuntimeError("gateway absent")

            def reqMarketDataType(self, _t: int) -> None:
                pass

            def disconnect(self) -> None:
                self.disconnects += 1

        def make_ib():
            ib = _FakeIBConn(should_fail=len(created) < 2)
            created.append(ib)
            return ib

        monkeypatch.setattr(ib_source, "_make_ib_instance", make_ib)
        monkeypatch.setattr(ib_source.time, "sleep", lambda seconds: sleeps.append(seconds))

        ib = ib_source.connect_ib(host="127.0.0.1", port=4002, client_id=99, attempts=3, backoff_seconds=0.25)

        assert ib is created[2]
        assert len(created) == 3
        assert [item.disconnects for item in created[:2]] == [1, 1]
        assert sleeps == [0.25, 0.5]

    def test_connect_echoue_proprement_apres_epuisement_des_retries(self, monkeypatch) -> None:
        from trader.market import ib_source

        created = []
        sleeps: list[float] = []

        class _FakeIBConn:
            def __init__(self) -> None:
                self.disconnects = 0

            def connect(self, *_a, **_kw) -> None:
                raise RuntimeError("gateway absent")

            def reqMarketDataType(self, _t: int) -> None:
                raise AssertionError("reqMarketDataType ne doit pas être appelé si connect échoue")

            def disconnect(self) -> None:
                self.disconnects += 1

        def make_ib():
            ib = _FakeIBConn()
            created.append(ib)
            return ib

        monkeypatch.setattr(ib_source, "_make_ib_instance", make_ib)
        monkeypatch.setattr(ib_source.time, "sleep", lambda seconds: sleeps.append(seconds))

        with pytest.raises(MarketError) as exc:
            ib_source.connect_ib(host="127.0.0.1", port=4002, client_id=99, attempts=3, backoff_seconds=0.1)

        assert exc.value.code == "ib_connect_failed"
        assert len(created) == 3
        assert [item.disconnects for item in created] == [1, 1, 1]
        assert sleeps == [0.1, 0.2]

    def test_req_market_data_type_exception_leve_ib_connect_failed(self, monkeypatch) -> None:
        """Si reqMarketDataType lève après un connect réussi → MarketError("ib_connect_failed")."""
        from trader.market import ib_source

        class _FakeIBConn:
            """Simule un IB qui se connecte mais dont reqMarketDataType explose."""
            def connect(self, *_a, **_kw) -> None:
                pass  # connect réussit
            def reqMarketDataType(self, _t: int) -> None:
                raise RuntimeError("socket fermée post-connect")

        monkeypatch.setattr(ib_source, "_make_ib_instance", lambda: _FakeIBConn())

        with pytest.raises(MarketError) as exc:
            ib_source.connect_ib(host="127.0.0.1", port=4002, client_id=99)
        assert exc.value.code == "ib_connect_failed"


# ---------------------------------------------------------------------------
# Finding 4 : LOOKBACK_MAP manque "1d"
# ---------------------------------------------------------------------------

class TestLookback1d:
    """Finding 4 : lookback='1d' doit mapper sur '1 D' sans erreur."""

    def test_lookback_1d_mappe_sur_1_D(self) -> None:
        from trader.market.ib_source import LOOKBACK_MAP
        assert LOOKBACK_MAP["1d"] == "1 D"

    def test_get_bars_avec_lookback_1d_ne_leve_pas(self) -> None:
        IBDataSource = _get_source()
        ib = FakeIB(bars_result=[_make_bar()])
        bars = IBDataSource(ib).get_bars("SPY", lookback="1d", interval="15m")
        assert len(bars) == 1
        call = ib.calls["reqHistoricalData"][-1]
        assert call["durationStr"] == "1 D"


# ---------------------------------------------------------------------------
# Test d'intégration réelle (skip si pas de Gateway)
# ---------------------------------------------------------------------------

_IB_INTEGRATION = os.getenv("IB_INTEGRATION_TEST") == "1"

@pytest.mark.skipif(not _IB_INTEGRATION, reason="Skip : IB_INTEGRATION_TEST=1 requis (Gateway paper sur 4002)")
def test_integration_reelle_spy_15m() -> None:
    """Test d'intégration : requiert Gateway paper sur 127.0.0.1:4002.
    Activer via : IB_INTEGRATION_TEST=1 uv run pytest tests/test_ib_source.py -k integration
    """
    from trader.market.ib_source import IBDataSource, connect_ib
    ib = connect_ib(host="127.0.0.1", port=4002, client_id=99)
    try:
        ds = IBDataSource(ib)
        bars = ds.get_bars("SPY", lookback="5d", interval="15m")
        assert len(bars) > 0
        assert bars[-1].ts.endswith("+00:00")
        assert bars[-1].close > 0
    finally:
        ib.disconnect()
