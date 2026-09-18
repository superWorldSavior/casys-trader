"""Pure, point-in-time capture of action-free market World episodes.

This module deliberately projects a *small whitelist* of market evidence.  It
does not accept a Trader context and never reads portfolio, decision,
scheduler, risk, prompt, tool, LLM, order, fill, quantity, or PnL state.
Those are controls or critic inputs, not inputs to an exogenous market
transition dataset.

The caller supplies the active/tradable cohort, bars already fetched for that
cohort, and explicitly selected market metadata.  A malformed point-in-time
proof does not turn into a guessed fresh observation: when a valid OHLCV anchor
is still available, the episode is retained with ``training_eligible=False``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from statistics import mean, pstdev

from trader.domain.world_episode import (
    AnchorBar,
    Freshness,
    MARKET_FEATURE_CONTRACT_ID,
    WorldEpisode,
    WorldObservation,
    completed_bar_cutoff,
    is_eligible_completed_bar,
    parse_bar_interval,
    parse_utc_timestamp,
)


UTC = timezone.utc
SAMPLING_POLICY_VERSION = "active_tradable_completed_bar.v1"

_CATEGORICAL_METADATA_ALIASES: tuple[tuple[str, str], ...] = (
    ("asset_family", "asset_family"),
    ("venue", "venue"),
    ("session_phase", "session_phase"),
    ("market_session", "session_phase"),
    ("session", "session_phase"),
    ("market_regime", "market_regime"),
    ("regime", "regime"),
    ("family_regime", "family_regime"),
    ("volatility_state", "volatility_state"),
    ("vol_state", "vol_state"),
    ("trend", "trend"),
)
_TIMESTAMP_SEMANTICS = {
    "bar_close": "bar_close",
    "bar_end": "bar_close",
    "close": "bar_close",
    "end": "bar_close",
    "bar_start": "bar_start",
    "start": "bar_start",
}


@dataclass(frozen=True)
class _MarketBar:
    """Validated market-only evidence used internally during capture."""

    ts: datetime
    end_at: datetime | None
    available_at: datetime | None
    available_at_invalid: bool
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    source_missing: bool
    interval: str | None
    timestamp_semantics: str

    @property
    def evidence_key(self) -> tuple[object, ...]:
        """A stable tie breaker that intentionally contains market evidence only."""

        return (
            self.ts,
            self.end_at or datetime.min.replace(tzinfo=UTC),
            self.available_at or datetime.min.replace(tzinfo=UTC),
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.source,
            self.interval or "",
            self.timestamp_semantics,
        )

    @property
    def order_key(self) -> tuple[object, ...]:
        return (
            self.end_at or self.ts,
            self.ts,
            self.available_at or datetime.min.replace(tzinfo=UTC),
            self.evidence_key,
        )


def capture_world_episodes(
    active_symbols: Iterable[object],
    tradable_symbols: Iterable[object] | None,
    bars_by_symbol: Mapping[object, Sequence[object] | Iterable[object]],
    market_metadata_by_symbol: Mapping[object, Mapping[str, object]] | None = None,
    *,
    source: str | None = None,
    interval: str | None = None,
    timestamp_semantics: str | None = None,
    captured_at: datetime | str | None = None,
    feature_contract_version: str = MARKET_FEATURE_CONTRACT_ID,
    sampling_policy_version: str = SAMPLING_POLICY_VERSION,
) -> tuple[WorldEpisode, ...]:
    """Project one deterministic episode for each active/tradable symbol.

    ``market_metadata_by_symbol`` is not a context blob.  Only the following
    top-level scalar market fields are read from it:

    * evidence: ``venue``, ``source``, ``interval``, ``timestamp_semantics``,
      ``available_at``;
    * point-in-time freshness: ``freshness``, ``freshness_status``, ``fresh``,
      ``stale``, ``stale_reason``, ``data_age_minutes``;
    * categorical market descriptors listed in
      :data:`_CATEGORICAL_METADATA_ALIASES`.

    Every other key is ignored, recursively and without serialising it.  The
    output is a tuple sorted by symbol so equivalent inputs yield the same
    order, identity, and payload hash.

    An episode cannot be fabricated when there is no valid OHLCV anchor at all:
    the domain contract rightly requires real anchor evidence.  Missing
    availability/freshness/timestamp-semantic proof *with* a valid anchor is
    represented as an explicitly non-trainable episode.
    """

    captured = _parse_timestamp(captured_at)
    captured_invalid = captured_at is not None and captured is None
    default_source = _text(source)
    default_interval = _text(interval)
    default_semantics = _text(timestamp_semantics)
    feature_version = _text(feature_contract_version) or MARKET_FEATURE_CONTRACT_ID
    sampling_version = _text(sampling_policy_version) or SAMPLING_POLICY_VERSION

    episodes: list[WorldEpisode] = []
    for symbol in _sampled_symbols(active_symbols, tradable_symbols):
        metadata = _symbol_metadata(market_metadata_by_symbol, symbol)
        fallback_source = _metadata_text(metadata, "source") or default_source
        fallback_interval = _metadata_text(metadata, "interval") or default_interval
        fallback_semantics = _metadata_text(metadata, "timestamp_semantics") or default_semantics
        metadata_available, metadata_available_invalid = _metadata_timestamp(metadata, "available_at")
        normalized_bars = _normalise_bars(
            _symbol_bars(bars_by_symbol, symbol),
            fallback_source=fallback_source,
            fallback_interval=fallback_interval,
            fallback_semantics=fallback_semantics,
            fallback_available_at=metadata_available,
            fallback_available_at_invalid=metadata_available_invalid,
        )
        anchor, ambiguous_anchor = _last_completed_bar(normalized_bars, captured)
        if anchor is None:
            # No positive, internally consistent OHLCV point can satisfy the
            # immutable AnchorBar contract.  Inventing one would be worse than
            # omitting this sampling slot.
            continue

        venue = _metadata_text(metadata, "venue")
        observation_venue = venue or "unknown"
        observation_interval = anchor.interval or "unknown"
        freshness = _freshness(metadata)
        categorical_features = _categorical_features(metadata)
        numeric_features, ambiguous_history = _numeric_features(normalized_bars, anchor)
        reason = _ineligibility_reason(
            anchor=anchor,
            captured_at=captured,
            captured_at_invalid=captured_invalid,
            venue_missing=venue is None,
            freshness=freshness,
            ambiguous_anchor=ambiguous_anchor or ambiguous_history,
        )
        observation = WorldObservation(
            venue=observation_venue,
            symbol=symbol,
            bar_interval=observation_interval,
            as_of_bar_ts=anchor.ts,
            feature_contract_version=feature_version,
            sampling_policy_version=sampling_version,
            anchor=AnchorBar(
                ts=anchor.ts,
                open=anchor.open,
                high=anchor.high,
                low=anchor.low,
                close=anchor.close,
                volume=anchor.volume,
                source=anchor.source,
                timestamp_semantics=anchor.timestamp_semantics,
            ),
            available_at=anchor.available_at,
            captured_at=captured,
            freshness=freshness,
            categorical_features=categorical_features,
            numeric_features=numeric_features,
        )
        episodes.append(
            WorldEpisode(
                observation=observation,
                training_eligible=reason is None,
                training_reason=reason,
            )
        )
    return tuple(episodes)


def _sampled_symbols(
    active_symbols: Iterable[object],
    tradable_symbols: Iterable[object] | None,
) -> tuple[str, ...]:
    active = {_text(value) for value in active_symbols}
    active.discard(None)
    if tradable_symbols is None:
        return tuple(sorted(active))
    tradable = {_text(value) for value in tradable_symbols}
    tradable.discard(None)
    return tuple(sorted(active.intersection(tradable)))


def _symbol_metadata(
    values: Mapping[object, Mapping[str, object]] | None,
    symbol: str,
) -> Mapping[str, object]:
    if not isinstance(values, Mapping):
        return {}
    candidate = values.get(symbol)
    return candidate if isinstance(candidate, Mapping) else {}


def _symbol_bars(
    values: Mapping[object, Sequence[object] | Iterable[object]],
    symbol: str,
) -> tuple[object, ...]:
    if not isinstance(values, Mapping):
        return ()
    candidate = values.get(symbol)
    if candidate is None or isinstance(candidate, (str, bytes, bytearray, Mapping)):
        return ()
    try:
        return tuple(candidate)
    except TypeError:
        return ()


def _normalise_bars(
    bars: Iterable[object],
    *,
    fallback_source: str | None,
    fallback_interval: str | None,
    fallback_semantics: str | None,
    fallback_available_at: datetime | None,
    fallback_available_at_invalid: bool,
) -> tuple[_MarketBar, ...]:
    normalized = [
        evidence
        for raw in bars
        if (
            evidence := _normalise_bar(
                raw,
                fallback_source=fallback_source,
                fallback_interval=fallback_interval,
                fallback_semantics=fallback_semantics,
                fallback_available_at=fallback_available_at,
                fallback_available_at_invalid=fallback_available_at_invalid,
            )
        )
        is not None
    ]
    return tuple(sorted(normalized, key=lambda value: value.order_key))


def _normalise_bar(
    raw: object,
    *,
    fallback_source: str | None,
    fallback_interval: str | None,
    fallback_semantics: str | None,
    fallback_available_at: datetime | None,
    fallback_available_at_invalid: bool,
) -> _MarketBar | None:
    ts = _parse_timestamp(_field(raw, "ts", "timestamp", "bar_ts"))
    if ts is None:
        return None
    values = {name: _finite_number(_field(raw, name)) for name in ("open", "high", "low", "close", "volume")}
    if any(value is None for value in values.values()):
        return None
    open_ = values["open"]
    high = values["high"]
    low = values["low"]
    close = values["close"]
    volume = values["volume"]
    if not _valid_ohlcv(open_, high, low, close, volume):
        return None

    supplied_source = _text(_field(raw, "source", "data_source")) or fallback_source
    source_missing = supplied_source is None or supplied_source.lower() == "unknown"
    supplied_interval = _text(_field(raw, "interval", "bar_interval")) or fallback_interval
    supplied_semantics = _text(_field(raw, "timestamp_semantics")) or fallback_semantics
    semantics = _timestamp_semantics(supplied_semantics)

    raw_available = _field(raw, "available_at", "observed_at", "ingested_at")
    available_at = _parse_timestamp(raw_available)
    available_at_invalid = _timestamp_is_invalid(raw_available)
    if raw_available is None:
        available_at = fallback_available_at
        available_at_invalid = fallback_available_at_invalid

    explicit_end = _field(raw, "bar_end_at", "end_at", "completed_at")
    end_at = _parse_timestamp(explicit_end)
    if end_at is not None and end_at < ts:
        return None
    if end_at is None:
        end_at = completed_bar_cutoff(
            as_of_bar_ts=ts,
            timestamp_semantics=semantics,
            bar_interval=supplied_interval,
        )
    if not is_eligible_completed_bar(
        ts=ts,
        bar_interval=supplied_interval,
        timestamp_semantics=semantics,
        end_at=end_at,
        available_at=available_at,
    ):
        return None

    return _MarketBar(
        ts=ts,
        end_at=end_at,
        available_at=available_at,
        available_at_invalid=available_at_invalid,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        source=supplied_source or "unknown",
        source_missing=source_missing,
        interval=supplied_interval,
        timestamp_semantics=semantics,
    )


def _last_completed_bar(
    bars: Sequence[_MarketBar],
    captured_at: datetime | None,
) -> tuple[_MarketBar | None, bool]:
    candidates: list[_MarketBar] = []
    for bar in bars:
        if captured_at is not None:
            completion = bar.end_at or bar.ts
            if completion > captured_at:
                continue
            # A provider stating that it made a bar available in the future is
            # hard evidence against its use at this capture point.  Missing
            # availability is retained later as an explicit non-trainable row.
            if bar.available_at is not None and bar.available_at > captured_at:
                continue
        candidates.append(bar)
    if not candidates:
        return None, False

    selected = max(candidates, key=lambda value: value.order_key)
    same_slot = [
        candidate
        for candidate in candidates
        if candidate.ts == selected.ts and (candidate.end_at or candidate.ts) == (selected.end_at or selected.ts)
    ]
    ambiguous = len({candidate.evidence_key for candidate in same_slot}) > 1
    return selected, ambiguous


def _numeric_features(
    bars: Sequence[_MarketBar],
    anchor: _MarketBar,
) -> tuple[dict[str, float], bool]:
    """Compute finite, causal features strictly from OHLCV evidence.

    The history is bounded, ordered by completed-bar time, and excludes a bar
    whose explicit availability is later than the anchor's availability.  No
    provider metadata is used in a numeric feature.  Besides the anchor-local
    keys, the seven ported formulas (volatility, z_score, efficiency_ratio,
    trend_slope, ohlc_volatility, atr_pct, range_position) read the held
    history only — a bar completing after the anchor can never move them.
    """

    anchor_end = anchor.end_at or anchor.ts
    candidates = [
        bar
        for bar in bars
        if (bar.end_at or bar.ts) <= anchor_end
        and (anchor.available_at is None or bar.available_at is None or bar.available_at <= anchor.available_at)
    ]
    candidates.sort(key=lambda value: value.order_key)
    deduped: dict[tuple[datetime, datetime], _MarketBar] = {}
    ambiguous = False
    for bar in candidates:
        key = (bar.ts, bar.end_at or bar.ts)
        previous = deduped.get(key)
        if previous is not None and previous.evidence_key != bar.evidence_key:
            ambiguous = True
        deduped[key] = bar
    history = list(sorted(deduped.values(), key=lambda value: value.order_key))[-20:]
    if not history:
        history = [anchor]
    if history[-1].evidence_key != anchor.evidence_key:
        history.append(anchor)
        history.sort(key=lambda value: value.order_key)
        history = history[-20:]

    latest = history[-1]
    features: dict[str, float] = {}
    _set_finite(features, "open_close_return", latest.close / latest.open - 1.0)
    _set_finite(features, "high_low_range_pct", (latest.high - latest.low) / latest.close)

    closes = [bar.close for bar in history]
    simple_returns = [
        closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes)) if closes[index - 1] > 0.0
    ]
    if simple_returns:
        _set_finite(features, "return", simple_returns[-1])
    if len(simple_returns) >= 2:
        mean_return = sum(simple_returns) / len(simple_returns)
        variance = sum((value - mean_return) ** 2 for value in simple_returns) / len(simple_returns)
        _set_finite(features, "realized_volatility", math.sqrt(variance))

    prior_volumes = [bar.volume for bar in history[:-1] if bar.volume > 0.0]
    if prior_volumes:
        mean_volume = sum(prior_volumes) / len(prior_volumes)
        if mean_volume > 0.0:
            _set_finite(features, "relative_volume", latest.volume / mean_volume)

    # Ported OHLCV formulas (source: trader/domain/market/features.py).
    # Every input below is the held causal `history` only — never a bar that
    # completes after the anchor or becomes available after it.
    if len(simple_returns) >= 2:
        _set_finite(features, "volatility", pstdev(simple_returns))
    if len(closes) >= 2:
        closes_sigma = pstdev(closes)
        if closes_sigma != 0.0:
            _set_finite(features, "z_score", (closes[-1] - mean(closes)) / closes_sigma)
        path_length = sum(abs(closes[index] - closes[index - 1]) for index in range(1, len(closes)))
        if path_length != 0.0:
            _set_finite(features, "efficiency_ratio", abs(closes[-1] - closes[0]) / path_length)
        slope = _causal_trend_slope(closes)
        if slope is not None:
            _set_finite(features, "trend_slope", slope)
    ohlc_volatility = _causal_ohlc_volatility(history)
    if ohlc_volatility is not None:
        _set_finite(features, "ohlc_volatility", ohlc_volatility)
    atr_pct = _causal_atr_pct(history)
    if atr_pct is not None:
        _set_finite(features, "atr_pct", atr_pct)
    highest = max(bar.high for bar in history)
    lowest = min(bar.low for bar in history)
    if highest > lowest:
        _set_finite(features, "range_position", (latest.close - lowest) / (highest - lowest))
    return features, ambiguous


def _causal_trend_slope(closes: Sequence[float]) -> float | None:
    """OLS slope of closes normalised by the absolute mean (needs >= 2 closes)."""

    if len(closes) < 2:
        return None
    count = len(closes)
    xs = list(range(count))
    mean_x = mean(xs)
    mean_close = mean(closes)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0.0 or mean_close == 0.0:
        return None
    slope = sum((x - mean_x) * (price - mean_close) for x, price in zip(xs, closes)) / denominator
    return slope / abs(mean_close)


def _causal_ohlc_volatility(history: Sequence[_MarketBar]) -> float | None:
    """Garman-Klass per-bar estimate averaged over the held history only."""

    if not history:
        return None
    estimates: list[float] = []
    for bar in history:
        if min(bar.open, bar.high, bar.low, bar.close) <= 0.0:
            continue
        high_low = math.log(bar.high / bar.low)
        close_open = math.log(bar.close / bar.open)
        estimate = 0.5 * high_low * high_low - (2.0 * math.log(2.0) - 1.0) * close_open * close_open
        estimates.append(max(0.0, estimate))
    if not estimates:
        return None
    return math.sqrt(mean(estimates))


def _causal_atr_pct(history: Sequence[_MarketBar]) -> float | None:
    """Mean true range over the held history, normalised by the latest close."""

    if not history:
        return None
    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in history:
        if not all(math.isfinite(value) for value in (bar.high, bar.low, bar.close)) or bar.high < bar.low:
            previous_close = bar.close if math.isfinite(bar.close) else previous_close
            continue
        candidates = [bar.high - bar.low]
        if previous_close is not None and math.isfinite(previous_close):
            candidates.extend((abs(bar.high - previous_close), abs(bar.low - previous_close)))
        true_ranges.append(max(candidates))
        previous_close = bar.close
    latest_close = history[-1].close
    if not true_ranges or not math.isfinite(latest_close) or latest_close <= 0.0:
        return None
    return mean(true_ranges) / latest_close


def _categorical_features(metadata: Mapping[str, object]) -> dict[str, str]:
    """Return only market/session/regime scalar categories from a fixed whitelist."""

    result: dict[str, str] = {}
    for metadata_key, feature_key in _CATEGORICAL_METADATA_ALIASES:
        if feature_key in result:
            continue
        value = _metadata_text(metadata, metadata_key)
        if value is not None:
            result[feature_key] = value
    return result


def _freshness(metadata: Mapping[str, object]) -> Freshness:
    """Read explicit market freshness evidence without inferring it from selection."""

    age = _finite_nonnegative(_metadata_value(metadata, "data_age_minutes"))
    raw = _metadata_value(metadata, "freshness")
    if isinstance(raw, Freshness):
        return raw
    if isinstance(raw, Mapping):
        status = _text(raw.get("status"))
        raw_age = _finite_nonnegative(raw.get("data_age_minutes"))
        if status in {"fresh", "stale", "missing", "unknown"}:
            return Freshness(status=status, data_age_minutes=raw_age if raw_age is not None else age)
    if isinstance(raw, str) and raw.strip().lower() in {"fresh", "stale", "missing", "unknown"}:
        return Freshness(status=raw.strip().lower(), data_age_minutes=age)

    status = _metadata_text(metadata, "freshness_status") or _metadata_text(metadata, "data_freshness")
    if status is not None and status.lower() in {"fresh", "stale", "missing", "unknown"}:
        return Freshness(status=status.lower(), data_age_minutes=age)
    if _metadata_value(metadata, "fresh") is True:
        return Freshness(status="fresh", data_age_minutes=age)
    if _metadata_value(metadata, "stale") is True or _metadata_text(metadata, "stale_reason") is not None:
        return Freshness(status="stale", data_age_minutes=age)
    return Freshness(status="unknown", data_age_minutes=age)


def _ineligibility_reason(
    *,
    anchor: _MarketBar,
    captured_at: datetime | None,
    captured_at_invalid: bool,
    venue_missing: bool,
    freshness: Freshness,
    ambiguous_anchor: bool,
) -> str | None:
    if captured_at is None:
        return "invalid_captured_at" if captured_at_invalid else "missing_captured_at"
    if venue_missing:
        return "missing_venue"
    if anchor.source_missing:
        return "missing_source"
    if parse_bar_interval(anchor.interval) is None:
        return "missing_or_invalid_interval"
    if anchor.available_at_invalid:
        return "invalid_available_at"
    if anchor.available_at is None:
        return "missing_available_at"
    if anchor.timestamp_semantics == "unknown":
        return "ambiguous_timestamp_semantics"
    if freshness.status != "fresh":
        return f"freshness_{freshness.status}"
    if ambiguous_anchor:
        return "ambiguous_bar_evidence"
    return None


def _metadata_timestamp(metadata: Mapping[str, object], name: str) -> tuple[datetime | None, bool]:
    raw = _metadata_value(metadata, name)
    return _parse_timestamp(raw), _timestamp_is_invalid(raw)


def _metadata_text(metadata: Mapping[str, object], name: str) -> str | None:
    return _text(_metadata_value(metadata, name))


def _metadata_value(metadata: Mapping[str, object], name: str) -> object:
    return metadata.get(name) if isinstance(metadata, Mapping) else None


def _field(value: object, *names: str) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _parse_timestamp(value: object) -> datetime | None:
    try:
        return parse_utc_timestamp(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _timestamp_is_invalid(value: object) -> bool:
    return value is not None and _parse_timestamp(value) is None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _finite_nonnegative(value: object) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number >= 0.0 else None


def _valid_ohlcv(
    open_: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    volume: float | None,
) -> bool:
    if None in (open_, high, low, close, volume):
        return False
    assert open_ is not None and high is not None and low is not None and close is not None and volume is not None
    return (
        min(open_, high, low, close) > 0.0
        and volume >= 0.0
        and high >= low
        and low <= open_ <= high
        and low <= close <= high
    )


def _timestamp_semantics(value: str | None) -> str:
    if value is None:
        return "unknown"
    return _TIMESTAMP_SEMANTICS.get(value.strip().lower(), "unknown")


def _set_finite(features: dict[str, float], key: str, value: float) -> None:
    if math.isfinite(value):
        features[key] = float(value)


__all__ = [
    "SAMPLING_POLICY_VERSION",
    "capture_world_episodes",
]
