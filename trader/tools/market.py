"""market — lecture des données marché.

v1 : yfinance (gratuit, US listed). Derrière une interface stable pour pouvoir
swapper vers une autre source (IB, LEAN, Polygon) plus tard.

Contrat : entrées minimales (symbole, lookback, interval), sortie machine-readable.
Aucune décision ici — uniquement de la donnée brute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Bar:
    ts: str  # ISO 8601
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: str


@dataclass(frozen=True)
class Freshness:
    """Verdict de fraîcheur d'une série de barres. `fresh=False` => ne pas trader."""

    fresh: bool
    reason: str | None        # None si frais ; sinon "no_data"|"unparseable_ts"|"too_old"
    age_minutes: float | None


# Tolérance d'horloge : une barre légèrement « dans le futur » (désync d'horloge
# entre yfinance et l'hôte) reste acceptable ; au-delà c'est une donnée invalide.
_CLOCK_SKEW_TOLERANCE_MINUTES = 5.0
_INTERVAL_MINUTES = {
    "5m": 5.0,
    "15m": 15.0,
    "30m": 30.0,
    "1h": 60.0,
    "4h": 4.0 * 60.0,
    "1d": 24.0 * 60.0,
}
_FRESHNESS_GRACE_MINUTES = 15.0
_FALLBACK_GROUP_SIZE_BY_TARGET = {"1h": 4, "4h": 4}


def freshness_budget_minutes(
    interval: str,
    *,
    grace_minutes: float = _FRESHNESS_GRACE_MINUTES,
) -> float:
    """Budget de fraîcheur = durée d'une barre + marge.

    Un marché live produit une nouvelle barre chaque `interval` ; au-delà de
    interval+grace, il est figé (fermé/halt). Intervalle inconnu => défaut 1h.
    """
    return _INTERVAL_MINUTES.get(interval, 60.0) + grace_minutes


def assess_freshness(bars: list[Bar], *, now: datetime, max_age_minutes: float) -> Freshness:
    """La fraîcheur EST le signal « marché live » : si la dernière barre est
    récente, le marché trade ; sinon (fermé/férié/weekend/halt) la donnée vieillit.

    Tout est comparé en UTC — aucune logique de fuseau/DST à se tromper. Fail-safe :
    pas de barres / `ts` imparsable / `ts` dans le futur / trop vieux => stale
    (jamais « frais par défaut »)."""
    if not bars:
        return Freshness(False, "no_data", None)
    ts = _parse_ts(str(bars[-1].ts))
    if ts is None:
        return Freshness(False, "unparseable_ts", None)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    delta_minutes = (now_utc - ts.astimezone(timezone.utc)).total_seconds() / 60.0
    if delta_minutes < -_CLOCK_SKEW_TOLERANCE_MINUTES:
        return Freshness(False, "future_ts", None)
    age_minutes = max(0.0, delta_minutes)
    if age_minutes > max_age_minutes:
        return Freshness(False, "too_old", age_minutes)
    return Freshness(True, None, age_minutes)


class MarketError(Exception):
    """Erreur d'accès marché. code machine-readable + contexte."""

    def __init__(self, code: str, context: str):
        self.code = code
        self.context = context
        super().__init__(f"{code}: {context}")


def _aggregate_sequential_bars(bars: list[Bar], *, group_size: int) -> list[Bar]:
    aggregated: list[Bar] = []
    for start in range(0, len(bars), group_size):
        group = bars[start : start + group_size]
        if len(group) < group_size:
            continue
        aggregated.append(_aggregate_group(group))
    return aggregated


def _bucket_key(ts: datetime, *, target_interval: str) -> tuple[object, int]:
    target_minutes = _interval_minutes(target_interval)
    if target_minutes is None:
        return (ts.date(), ts.hour)
    if target_minutes >= _INTERVAL_MINUTES["1d"]:
        return (ts.date(), 0)
    minutes_since_midnight = ts.hour * 60 + ts.minute
    return (ts.date(), int(minutes_since_midnight // target_minutes))


def _interval_minutes(interval: str | None) -> float | None:
    if interval is None:
        return None
    known = _INTERVAL_MINUTES.get(interval)
    if known is not None:
        return known
    if len(interval) < 2:
        return None
    try:
        amount = float(interval[:-1])
    except ValueError:
        return None
    unit = interval[-1]
    if unit == "m":
        return amount
    if unit == "h":
        return amount * 60.0
    if unit == "d":
        return amount * 24.0 * 60.0
    return None


def _infer_source_minutes(parsed: list[datetime | None]) -> float | None:
    if any(ts is None for ts in parsed):
        return None
    deltas: list[float] = []
    previous: datetime | None = None
    for ts in parsed:
        if previous is not None and ts is not None:
            try:
                delta = (ts - previous).total_seconds() / 60.0
            except TypeError:
                return None
            if delta > 0:
                deltas.append(delta)
        previous = ts
    return min(deltas) if deltas else None


def _aggregate_group_size(
    *,
    parsed: list[datetime | None],
    target_interval: str,
    source_interval: str | None,
) -> int | None:
    target_minutes = _interval_minutes(target_interval)
    if target_minutes is None:
        return None
    source_minutes = _interval_minutes(source_interval)
    if source_minutes is None:
        source_minutes = _infer_source_minutes(parsed)
    if source_minutes is None:
        return _FALLBACK_GROUP_SIZE_BY_TARGET.get(target_interval)
    if source_minutes <= 0 or target_minutes <= source_minutes:
        return None
    ratio = target_minutes / source_minutes
    group_size = round(ratio)
    if group_size < 1 or abs(ratio - group_size) > 1e-9:
        return None
    return int(group_size)


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _aggregate_group(group: list[Bar]) -> Bar:
    return Bar(
        ts=group[-1].ts,
        open=group[0].open,
        high=max(bar.high for bar in group),
        low=min(bar.low for bar in group),
        close=group[-1].close,
        volume=sum(bar.volume for bar in group),
    )


def aggregate_bars(
    bars: list[Bar],
    *,
    target_interval: str,
    source_interval: str | None = None,
) -> list[Bar]:
    """Aggregate smaller bars into a governed semantic timeframe."""
    parsed = [_parse_ts(bar.ts) for bar in bars]
    group_size = _aggregate_group_size(
        parsed=parsed,
        target_interval=target_interval,
        source_interval=source_interval,
    )
    if group_size is None:
        return bars
    if any(ts is None for ts in parsed):
        return _aggregate_sequential_bars(bars, group_size=group_size)

    buckets: dict[tuple[object, int], list[Bar]] = {}
    for bar, ts in zip(bars, parsed, strict=True):
        assert ts is not None
        bucket = _bucket_key(ts, target_interval=target_interval)
        buckets.setdefault(bucket, []).append(bar)

    aggregated: list[Bar] = []
    for group in buckets.values():
        if len(group) < group_size:
            continue
        aggregated.append(_aggregate_group(group))
    return aggregated


def get_bars(symbol: str, lookback: str = "5d", interval: str = "1h") -> list[Bar]:
    """Barres OHLCV. lookback ex: '1d','5d','1mo'; interval ex: '1h','1d'.

    Lève MarketError(code='no_data'|'fetch_failed') en cas d'échec — jamais de
    retour silencieux vide ambigu.
    """
    import yfinance as yf

    source_interval = "1h" if interval == "4h" else interval

    try:
        df = yf.Ticker(symbol).history(period=lookback, interval=source_interval, auto_adjust=False)
    except Exception as e:  # noqa: BLE001 — frontière externe
        raise MarketError("fetch_failed", f"{symbol}: {e}") from e

    if df is None or df.empty:
        raise MarketError("no_data", f"{symbol} (lookback={lookback}, interval={interval})")

    bars: list[Bar] = []
    for idx, row in df.iterrows():
        bars.append(
            Bar(
                ts=idx.isoformat(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
            )
        )
    if source_interval == interval:
        return bars
    return aggregate_bars(bars, target_interval=interval, source_interval=source_interval)


def get_quote(symbol: str) -> Quote:
    """Dernier prix connu (close de la dernière barre intraday)."""
    bars = get_bars(symbol, lookback="1d", interval="1h")
    last = bars[-1]
    return Quote(symbol=symbol, price=last.close, ts=last.ts)
