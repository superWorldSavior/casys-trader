"""market — lecture des données marché.

v1 : yfinance (gratuit, US listed). Derrière une interface stable pour pouvoir
swapper vers une autre source (IB, LEAN, Polygon) plus tard.

Contrat : entrées minimales (symbole, lookback, interval), sortie machine-readable.
Aucune décision ici — uniquement de la donnée brute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


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


# --- Anticipation de l'ouverture de session (alignement du réveil) -----------
# Heure d'ouverture de la session régulière, en heure LOCALE de la place de
# cotation du symbole. zoneinfo gère DST → conversion UTC correcte toute
# l'année, sans pytz. Sans symbole (ou symbole non mappé) : défaut US.
_MARKET_TZ = ZoneInfo("America/New_York")
_SESSION_OPEN_HOUR = 9
_SESSION_OPEN_MINUTE = 30
# Place de cotation par suffixe yfinance → (tz, open_h, open_m, close_h, close_m).
# Le FX (=X) n'est volontairement PAS mappé : marché ~24h/5, le calendrier US
# par défaut reste le comportement historique le moins faux pour le réveil.
_Venue = tuple[ZoneInfo, int, int, int, int]
_VENUE_US: _Venue = (_MARKET_TZ, _SESSION_OPEN_HOUR, _SESSION_OPEN_MINUTE, 16, 0)
_VENUE_BY_SUFFIX: dict[str, _Venue] = {
    ".TW": (ZoneInfo("Asia/Taipei"), 9, 0, 13, 30),     # TWSE (pas de DST)
    ".TWO": (ZoneInfo("Asia/Taipei"), 9, 0, 13, 30),    # Taipei Exchange (pas de DST)
    ".PA": (ZoneInfo("Europe/Paris"), 9, 0, 17, 30),    # Euronext Paris
    ".DE": (ZoneInfo("Europe/Berlin"), 9, 0, 17, 30),   # XETRA
    ".AS": (ZoneInfo("Europe/Amsterdam"), 9, 0, 17, 30), # Euronext Amsterdam
    ".BR": (ZoneInfo("Europe/Brussels"), 9, 0, 17, 30),  # Euronext Brussels
    ".LS": (ZoneInfo("Europe/Lisbon"), 8, 0, 16, 30),    # Euronext Lisbon
    ".SW": (ZoneInfo("Europe/Zurich"), 9, 0, 17, 30),    # SIX Swiss
    ".MI": (ZoneInfo("Europe/Rome"), 9, 0, 17, 30),      # Borsa Italiana
    ".MC": (ZoneInfo("Europe/Madrid"), 9, 0, 17, 30),    # BME Madrid
    ".L": (ZoneInfo("Europe/London"), 8, 0, 16, 30),     # LSE
    ".CO": (ZoneInfo("Europe/Copenhagen"), 9, 0, 17, 0), # Nasdaq Copenhagen
    ".ST": (ZoneInfo("Europe/Stockholm"), 9, 0, 17, 30), # Nasdaq Stockholm
    ".HE": (ZoneInfo("Europe/Helsinki"), 10, 0, 18, 30), # Nasdaq Helsinki
    ".VI": (ZoneInfo("Europe/Vienna"), 9, 0, 17, 30),    # Wiener Börse
    ".OL": (ZoneInfo("Europe/Oslo"), 9, 0, 16, 20),      # Oslo Børs
}
_VENUE_BY_SYMBOL: dict[str, _Venue] = {
    "^FCHI": (ZoneInfo("Europe/Paris"), 9, 0, 17, 30),  # CAC 40 — coté à Paris
}


def _venue_for_symbol(symbol: str | None) -> _Venue:
    if symbol:
        exact = _VENUE_BY_SYMBOL.get(symbol)
        if exact is not None:
            return exact
        for suffix, venue in _VENUE_BY_SUFFIX.items():
            if symbol.endswith(suffix):
                return venue
    return _VENUE_US


def session_snapshot(symbol: str, *, now: datetime) -> dict:
    """Où en est la session régulière du marché du symbole — fait calculé par le
    code et injecté dans le contexte LLM (le prompt n'a pas à lister les horaires
    des places).

    Retour : {"open": bool, "since_open_m": int|None, "to_close_m": int|None}.
    FX (=X) : ~24h/5 → open=True en semaine sans bornes, fermé le weekend.
    Compromis assumé (comme le calendrier de réveil) : fériés et demi-journées
    non gérés ; weekend = samedi/dimanche locaux de la place.
    """
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if symbol.endswith("=X"):
        is_weekend = now_utc.weekday() >= 5
        return {"open": not is_weekend, "since_open_m": None, "to_close_m": None}
    tz, open_hour, open_minute, close_hour, close_minute = _venue_for_symbol(symbol)
    local_now = now_utc.astimezone(tz)
    if local_now.weekday() >= 5:
        return {"open": False, "since_open_m": None, "to_close_m": None}
    open_dt = local_now.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
    close_dt = local_now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
    if not (open_dt <= local_now < close_dt):
        return {"open": False, "since_open_m": None, "to_close_m": None}
    return {
        "open": True,
        "since_open_m": int((local_now - open_dt).total_seconds() // 60),
        "to_close_m": int((close_dt - local_now).total_seconds() // 60),
    }
# On veut être réveillé un peu AVANT la cloche : fetch + décider pile à l'ouverture.
PRE_OPEN_LEAD_MINUTES = 5.0
# Fenêtre de grâce APRÈS la cloche : tant que la donnée est encore stale (latence
# fournisseur / data IB différée), on continue à poller serré plutôt que de
# retomber dans le backoff long et rater le début de séance.
OPEN_GRACE_MINUTES = 20.0
# Cadence du polling serré pendant lead + grace (le scheduler dort min(wake, poll)).
_TIGHT_POLL_MINUTES = 1.0


def next_regular_session_open(now: datetime, *, symbol: str | None = None) -> datetime:
    """Prochaine ouverture de session régulière (strictement > now), en UTC.

    Le calendrier est celui de la place de cotation du `symbol` (TWSE, Euronext
    Paris, XETRA) ; sans symbole ou symbole non mappé → US. Compromis assumé :
    ne gère PAS les jours fériés ni les demi-journées — seuls les weekends
    (samedi=5, dimanche=6) sont sautés. DST géré via zoneinfo. Déterministe :
    `now` est injecté, aucune dépendance cachée à l'horloge.
    """
    tz, open_hour, open_minute, _close_h, _close_m = _venue_for_symbol(symbol)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(tz)
    candidate = local_now.replace(
        hour=open_hour, minute=open_minute, second=0, microsecond=0
    )
    # Strictement après now ET un jour ouvré (weekday 5=sam, 6=dim sautés).
    while candidate <= local_now or candidate.weekday() >= 5:
        candidate = (candidate + timedelta(days=1)).replace(
            hour=open_hour, minute=open_minute, second=0, microsecond=0
        )
    return candidate.astimezone(timezone.utc)


def most_recent_session_open(now: datetime, *, symbol: str | None = None) -> datetime:
    """Ouverture régulière de la session courante/la plus récente (<= now), UTC.

    Symétrique de `next_regular_session_open` : recule jour par jour en sautant
    les weekends. Sert à détecter la fenêtre de grâce post-cloche.
    """
    tz, open_hour, open_minute, _close_h, _close_m = _venue_for_symbol(symbol)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(tz)
    candidate = local_now.replace(
        hour=open_hour, minute=open_minute, second=0, microsecond=0
    )
    while candidate > local_now or candidate.weekday() >= 5:
        candidate = (candidate - timedelta(days=1)).replace(
            hour=open_hour, minute=open_minute, second=0, microsecond=0
        )
    return candidate.astimezone(timezone.utc)


def clamp_wake_to_session_open(
    wake_minutes: float,
    *,
    now: datetime,
    symbol: str | None = None,
    lead_minutes: float = PRE_OPEN_LEAD_MINUTES,
    grace_minutes: float = OPEN_GRACE_MINUTES,
) -> float:
    """Borne un next-wake pour rester aligné sur l'ouverture de marché.

    Deux garde-fous, dans cet ordre :
      1. Grâce post-cloche : si on est dans [open, open+grace], on poll serré
         (la donnée est encore stale → latence fournisseur, on attend les barres).
      2. Anti-« rater la cloche » : sinon on borne pour se réveiller `lead_minutes`
         avant la prochaine ouverture ; loin de l'open, `wake_minutes` est intact.

    Plancher à 1min sur tous les chemins : jamais 0/négatif (réveil immédiat).
    """
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    grace_end = most_recent_session_open(now_utc, symbol=symbol) + timedelta(
        minutes=grace_minutes
    )
    if now_utc <= grace_end:
        return max(1.0, min(wake_minutes, _TIGHT_POLL_MINUTES))
    target = next_regular_session_open(now_utc, symbol=symbol) - timedelta(
        minutes=lead_minutes
    )
    capped_minutes = (target - now_utc).total_seconds() / 60.0
    # min() = on garde le plus court entre backoff courant et « avant la cloche ».
    # max(1.0, ...) plancher sur les DEUX chemins : un wake_minutes <= 0 (ex. CLI
    # mal validé) ne doit jamais produire un réveil immédiat/négatif.
    return max(1.0, min(wake_minutes, capped_minutes))


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


def classify_symbol_context(
    *,
    runtime_interval: str,
    has_runtime_price: bool,
    is_runtime_stale: bool,
    session_open: bool,
    daily_fresh: bool,
    last_runtime_bar_ts: str | None = None,
    data_age_minutes: float | None = None,
    daily_as_of: str | None = None,
    next_session_open: str | None = None,
) -> dict:
    """Sépare deux capacités d'un symbole (design §5.1) — fonction pure.

    - ``execution.enabled`` : on peut passer un ordre. Exige un prix runtime FRAIS,
      une session tradable et l'absence de stale. ``False`` interdit tout fill.
    - ``planning.enabled`` : on peut analyser / poser une veille / planifier un
      réveil. Vrai dès que le contexte daily/swing est valide, même runtime stale.

    ``reason`` d'exécution, par priorité déterministe : ``session_closed`` (marché
    fermé — le stale en découle) > ``runtime_stale`` (séance ouverte, données en
    retard) > ``no_price`` ; ``tradable`` quand activé.
    """
    execution_enabled = has_runtime_price and session_open and not is_runtime_stale
    if execution_enabled:
        execution_reason = "tradable"
    elif not session_open:
        execution_reason = "session_closed"
    elif is_runtime_stale:
        execution_reason = "runtime_stale"
    else:
        execution_reason = "no_price"

    return {
        "execution": {
            "enabled": execution_enabled,
            "reason": execution_reason,
            "runtime_interval": runtime_interval,
            "last_runtime_bar_ts": last_runtime_bar_ts,
            "data_age_minutes": data_age_minutes,
        },
        "planning": {
            "enabled": bool(daily_fresh),
            "reason": "daily_context_fresh" if daily_fresh else "daily_stale",
            "daily_as_of": daily_as_of,
            "next_session_open": next_session_open,
        },
    }
