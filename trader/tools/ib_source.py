"""ib_source — connecteur Interactive Brokers pour casys-trader.

Expose IBDataSource derrière la même interface Bar que market.get_bars.
Source opt-in : yfinance reste la source par défaut.

Usage :
    from trader.tools.ib_source import IBDataSource, connect_ib

    # Option A : client connecté en amont (production)
    ib = connect_ib(host="127.0.0.1", port=4002, client_id=17)
    ds = IBDataSource(ib)
    bars = ds.get_bars("SPY", lookback="5d", interval="15m")
    ib.disconnect()

    # Option B : client injecté (tests, mock)
    ds = IBDataSource(fake_ib_client)
    bars = ds.get_bars("EURUSD=X", lookback="1mo", interval="1h")

Contrats : config/ib_contracts.yaml (tickers yfinance → spec IB).
Barres : toujours list[Bar] avec ts ISO 8601 UTC.
Erreurs : toujours MarketError(code, context) — jamais d'exception ib_async brute.

Conventions :
    AX / Machine-Readable Errors : code court + context lisible machine.
    AX / Explicit over Implicit  : les tables de mapping sont des constantes nommées.
    AX / Fail-Fast               : mapping inconnu → MarketError immédiat.
"""

from __future__ import annotations

import time
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trader.domain.market_data import Bar, MarketError

# ---------------------------------------------------------------------------
# Tables de mapping (constantes nommées, AX: Explicit over Implicit)
# ---------------------------------------------------------------------------

#: Intervalle interne casys-trader → barSizeSetting IB
INTERVAL_MAP: dict[str, str] = {
    "15m": "15 mins",
    "30m": "30 mins",
    "1h":  "1 hour",
    "4h":  "4 hours",
    "1d":  "1 day",
}

#: Lookback interne casys-trader → durationStr IB
LOOKBACK_MAP: dict[str, str] = {
    "1d":  "1 D",
    "5d":  "5 D",
    "1mo": "1 M",
    "3mo": "3 M",
    "6mo": "6 M",
    "1y":  "1 Y",
}

#: whatToShow par sec_type (MIDPOINT pour CASH/Forex ; AGGTRADES pour crypto IBKR/Paxos)
_WHAT_TO_SHOW: dict[str, str] = {
    "STK":     "TRADES",
    "IND":     "TRADES",
    "CONTFUT": "TRADES",
    "CRYPTO":  "AGGTRADES",
    "CASH":    "MIDPOINT",
}

# Chemin par défaut du YAML de mapping (relatif à la racine du repo)
_DEFAULT_CONTRACTS_PATH = "config/ib_contracts.yaml"


# ---------------------------------------------------------------------------
# IBDataSource
# ---------------------------------------------------------------------------

class IBDataSource:
    """Source de barres IB derrière l'interface Bar de market.py.

    Le client `ib` est INJECTÉ — aucune connexion n'est établie ici.
    Utilisez connect_ib() pour créer un vrai client, ou un FakeIB pour les tests.
    """

    def __init__(
        self,
        ib: Any,
        *,
        contracts_path: str = _DEFAULT_CONTRACTS_PATH,
        reconnect_factory: Any | None = None,
    ) -> None:
        self._ib = ib
        self._contracts = self._load_contracts(contracts_path)
        self._reconnect_factory = reconnect_factory

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "15m",
    ) -> list[Bar]:
        """Barres OHLCV IB pour symbol.

        Args:
            symbol:   Ticker interne (format yfinance, ex: 'SPY', 'EURUSD=X', 'CL=F').
            lookback: Période d'historique ('5d','1mo','3mo','6mo','1y').
            interval: Taille de barre ('15m','30m','1h','4h','1d').

        Returns:
            list[Bar] avec ts en ISO 8601 UTC.

        Raises:
            MarketError: code ∈ {'unmapped_symbol','unsupported_interval',
                                  'unsupported_lookback','ib_qualify_failed',
                                  'ib_fetch_failed','ib_no_data'}.
        """
        try:
            return self._get_bars_once(symbol, lookback=lookback, interval=interval)
        except MarketError as exc:
            if self._reconnect_factory is None or not self._is_retriable_connection_error(exc):
                raise
            self._reconnect()
            return self._get_bars_once(symbol, lookback=lookback, interval=interval)

    def disconnect(self) -> None:
        self._disconnect_ib(self._ib)

    def _reconnect(self) -> None:
        self.disconnect()
        self._ib = self._reconnect_factory()

    def _get_bars_once(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "15m",
    ) -> list[Bar]:
        # 1. Mapping unités (fail-fast)
        bar_size = self._resolve_interval(interval)
        duration  = self._resolve_lookback(lookback)

        # 2. Construction du contrat (lève MarketError si symbole absent)
        contract = self._build_contract(symbol)

        # 3. Résolution de whatToShow selon sec_type
        spec = self._contracts[symbol]
        what_to_show = _WHAT_TO_SHOW.get(spec["sec_type"], "TRADES")

        # 4. Qualification du contrat
        # Finding 2 : envelopper l'appel — une socket cassée ou RequestError ne doit
        # jamais remonter en exception brute.
        try:
            qualified = self._ib.qualifyContracts(contract)
        except Exception as exc:  # noqa: BLE001 — frontière externe ib_async
            raise MarketError(
                "ib_qualify_failed",
                f"{symbol}: qualifyContracts a levé {type(exc).__name__}: {exc}",
            ) from exc

        # Finding 1 : liste vide, élément None, ou conId=0/absent → non qualifié.
        # qualifyContracts retourne parfois [None] ou un contrat avec conId=0
        # pour un contrat qu'il n'a pas pu résoudre (pas toujours []).
        first = qualified[0] if qualified else None
        if first is None or getattr(first, "conId", None) in (None, 0):
            raise MarketError(
                "ib_qualify_failed",
                f"{symbol}: contrat non résolu (qualifyContracts={qualified!r})",
            )

        # 5. Requête historique
        try:
            raw_bars = self._ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=False,
                formatDate=2,
            )
        except Exception as exc:  # noqa: BLE001 — frontière externe ib_async
            raise MarketError("ib_fetch_failed", f"{symbol}: {exc}") from exc

        # 6. Série vide → erreur explicite
        if not raw_bars:
            raise MarketError("ib_no_data", f"{symbol} (lookback={lookback}, interval={interval})")

        # 7. Conversion → Bar[]
        return [self._to_bar(b) for b in raw_bars]

    @staticmethod
    def _disconnect_ib(ib: Any) -> None:
        disconnect = getattr(ib, "disconnect", None)
        if disconnect is None:
            return
        try:
            disconnect()
        except Exception:  # noqa: BLE001 - déconnexion best-effort
            return

    @staticmethod
    def _is_retriable_connection_error(exc: MarketError) -> bool:
        if exc.code not in {"ib_fetch_failed", "ib_qualify_failed"}:
            return False
        context = exc.context.lower()
        markers = (
            "connection",
            "connexion",
            "socket",
            "disconnect",
            "closed",
            "ferm",
            "reset",
            "broken pipe",
            "peer",
        )
        return any(marker in context for marker in markers)

    # ------------------------------------------------------------------
    # Méthodes internes (utilisées aussi directement dans les tests)
    # ------------------------------------------------------------------

    def _build_contract(self, symbol: str) -> Any:
        """Construit l'objet Contract ib_async correspondant au symbole.

        Raises:
            MarketError("unmapped_symbol"): si symbol absent de contracts_path.
        """
        if symbol not in self._contracts:
            raise MarketError("unmapped_symbol", f"{symbol}")

        spec = self._contracts[symbol]
        sec_type = spec["sec_type"]

        try:
            from ib_async import ContFuture, Crypto, Forex, Index, Stock
        except ImportError as exc:
            raise MarketError("ib_import_failed", "ib_async non installé") from exc

        if sec_type == "STK":
            return Stock(
                symbol=spec["symbol"],
                exchange=spec["exchange"],
                currency=spec["currency"],
            )
        if sec_type == "CASH":
            # Convention IB Forex : pair = base + quote (ex: EUR + USD = EURUSD)
            pair = spec["symbol"] + spec["currency"]
            return Forex(pair=pair, exchange=spec["exchange"])
        if sec_type == "IND":
            return Index(
                symbol=spec["symbol"],
                exchange=spec["exchange"],
                currency=spec["currency"],
            )
        if sec_type == "CONTFUT":
            return ContFuture(
                symbol=spec["symbol"],
                exchange=spec["exchange"],
                currency=spec["currency"],
            )
        if sec_type == "CRYPTO":
            return Crypto(
                symbol=spec["symbol"],
                exchange=spec["exchange"],
                currency=spec["currency"],
            )

        # sec_type inconnu (jamais atteint si le YAML est valide)
        raise MarketError("unsupported_sec_type", f"{symbol}: sec_type={sec_type!r}")

    # ------------------------------------------------------------------
    # Helpers privés
    # ------------------------------------------------------------------

    @staticmethod
    def _load_contracts(path: str) -> dict[str, dict]:
        """Charge config/ib_contracts.yaml et retourne la table `contracts`."""
        p = Path(path)
        if not p.is_absolute():
            # Chemin relatif → résoudre depuis la racine du repo (parent de trader/)
            root = Path(__file__).parent.parent.parent
            p = root / path
        with p.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data.get("contracts", {})

    @staticmethod
    def _resolve_interval(interval: str) -> str:
        """Traduit l'interval interne en barSizeSetting IB.

        Raises:
            MarketError("unsupported_interval"): si interval inconnu.
        """
        if interval not in INTERVAL_MAP:
            raise MarketError(
                "unsupported_interval",
                f"{interval!r} : valeurs supportées = {sorted(INTERVAL_MAP)}",
            )
        return INTERVAL_MAP[interval]

    @staticmethod
    def _resolve_lookback(lookback: str) -> str:
        """Traduit le lookback interne en durationStr IB.

        Raises:
            MarketError("unsupported_lookback"): si lookback inconnu.
        """
        if lookback not in LOOKBACK_MAP:
            raise MarketError(
                "unsupported_lookback",
                f"{lookback!r} : valeurs supportées = {sorted(LOOKBACK_MAP)}",
            )
        return LOOKBACK_MAP[lookback]

    @staticmethod
    def _to_bar(ib_bar: Any) -> Bar:
        """Convertit un BarData ib_async en Bar casys-trader (ts ISO 8601 UTC).

        ib_async avec formatDate=2 :
          - intraday : bar.date = datetime aware UTC
          - daily    : bar.date = datetime.date (naïf) → on ajoute UTC minuit
        """
        raw_date = ib_bar.date
        if isinstance(raw_date, datetime):
            if raw_date.tzinfo is None:
                raw_date = raw_date.replace(tzinfo=timezone.utc)
            ts = raw_date.astimezone(timezone.utc).isoformat()
        elif isinstance(raw_date, _date):
            # datetime.date → datetime UTC minuit
            ts = datetime(
                raw_date.year, raw_date.month, raw_date.day,
                tzinfo=timezone.utc,
            ).isoformat()
        else:
            # Fallback string : pas attendu avec formatDate=2 mais défensif
            ts = str(raw_date)

        return Bar(
            ts=ts,
            open=float(ib_bar.open),
            high=float(ib_bar.high),
            low=float(ib_bar.low),
            close=float(ib_bar.close),
            volume=float(ib_bar.volume),
        )


# ---------------------------------------------------------------------------
# Helper de connexion réelle (NON couvert par les tests sans réseau)
# ---------------------------------------------------------------------------

def _make_ib_instance() -> Any:
    """Instancie ib_async.IB. Séparé pour permettre le monkey-patch dans les tests."""
    try:
        from ib_async import IB
    except ImportError as exc:
        raise MarketError("ib_import_failed", "ib_async non installé") from exc
    return IB()


def connect_ib(
    host: str = "127.0.0.1",
    port: int = 4002,
    client_id: int = 17,
    *,
    market_data_type: int = 3,
    timeout: float = 15.0,
    attempts: int = 3,
    backoff_seconds: float = 0.5,
) -> Any:
    """Crée et connecte un client ib_async.IB au Gateway/TWS.

    Args:
        host:             Adresse du Gateway (défaut: localhost).
        port:             Port API (4002 = paper, 7497 = live paper TWS).
        client_id:        ID client unique (éviter les conflits multi-process).
        market_data_type: 1=live, 3=delayed, 4=delayed-frozen (défaut: 3).
        timeout:          Timeout de connexion en secondes.
        attempts:         Nombre de tentatives avant abandon.
        backoff_seconds:  Délai initial entre tentatives, doublé à chaque retry.

    Returns:
        ib_async.IB connecté.

    Raises:
        MarketError("ib_connect_failed"): si la connexion échoue (connect ou
            reqMarketDataType).
    """
    attempts = max(1, int(attempts))
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        ib = _make_ib_instance()
        # Finding 3 : tout le bloc post-instanciation est enveloppé — une exception
        # dans reqMarketDataType (socket cassée entre connect et la requête) ne doit
        # pas remonter en brut.
        try:
            ib.connect(host, port, clientId=client_id, timeout=timeout)
            ib.reqMarketDataType(market_data_type)
            return ib
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            IBDataSource._disconnect_ib(ib)
            if attempt < attempts:
                time.sleep(backoff_seconds * (2 ** (attempt - 1)))

    assert last_exc is not None
    raise MarketError(
        "ib_connect_failed",
        f"{host}:{port} clientId={client_id}: {type(last_exc).__name__}: {last_exc}",
    ) from last_exc
