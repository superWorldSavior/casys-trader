"""Pure, fixed-horizon market labels for World episodes.

This module deliberately knows only an observation anchor and exogenous market
bars.  It does not import decision, broker, scheduler, queue, or FLAIR code.
Inputs are duck-typed so the application layer can consume the immutable
``trader.domain.world_episode`` contract without coupling the labeler to a
store or runtime implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from trader.domain.world_episode import (
    WORLD_OUTCOME_SCHEMA_VERSION,
    canonical_sha256,
    completed_bar_cutoff,
    is_eligible_completed_bar,
    parse_bar_interval,
    world_outcome_event_id,
)


UTC = timezone.utc
LABEL_SEMANTICS_VERSION = "world_elapsed_labeler.v1"
DIRECTION_SEMANTICS_VERSION = "simple_return_band_50bp.v1"
DIRECTION_BAND = 0.005
_SKIPPED_NON_BAR = object()


@dataclass(frozen=True)
class HorizonSpec:
    """One explicit elapsed-time label contract."""

    horizon_id: str
    duration: timedelta


ELAPSED_4H = HorizonSpec("elapsed_4h.v1", timedelta(hours=4))
ELAPSED_1D = HorizonSpec("elapsed_1d.v1", timedelta(days=1))
DEFAULT_HORIZONS: tuple[HorizonSpec, ...] = (ELAPSED_4H, ELAPSED_1D)
_HORIZONS_BY_ID = {spec.horizon_id: spec for spec in DEFAULT_HORIZONS}


@dataclass(frozen=True)
class _BarEvidence:
    """Normalized, immutable view of one provider bar."""

    ts: datetime
    end_at: datetime
    available_at: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    source: str
    interval: str | None
    timestamp_semantics: str
    price_basis: str | None
    availability_provenance: str
    fingerprint: str
    evidence_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": _iso(self.ts),
            "bar_end_at": _iso(self.end_at),
            "available_at": _iso(self.available_at),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "source": self.source,
            "interval": self.interval,
            "timestamp_semantics": self.timestamp_semantics,
            "price_basis": self.price_basis,
            "availability_provenance": self.availability_provenance,
            "fingerprint": self.fingerprint,
            "evidence_id": self.evidence_id,
        }


def label_episode(
    episode: object,
    bars: Iterable[object],
    *,
    now: datetime | str,
    horizons: Sequence[object] = DEFAULT_HORIZONS,
) -> list[dict[str, object]]:
    """Return one independent label result per requested fixed horizon.

    ``now`` is injected so this is deterministic and side-effect free.  A
    horizon is never allowed to borrow an endpoint from another horizon.
    """

    materialized_bars = tuple(bars)
    return [label_horizon(episode, materialized_bars, horizon, now=now) for horizon in horizons]


def label_horizon(
    episode: object,
    bars: Iterable[object],
    horizon: object,
    *,
    now: datetime | str,
) -> dict[str, object]:
    """Label exactly one elapsed horizon from market bars only.

    The immutable observation identity is ``observation.as_of_bar_ts`` (or the
    same field on a mapping/duck-type episode), never the computation clock.
    The causal transition clock is the anchor's completed ``end_at``: it is
    identical for ``bar_close`` evidence and advances by one source interval
    for ``bar_start`` evidence.  The first complete endpoint at or after
    ``target_at`` is eligible only through one source-bar interval of lateness.
    """

    spec = _coerce_horizon(horizon)
    computed_at = _parse_datetime(now)
    observation = _field(episode, "observation")
    episode_id = _episode_id(episode, observation)
    horizon_id = _horizon_id(horizon)

    if spec is None:
        return _unknown_result(
            episode_id=episode_id,
            horizon_id=horizon_id,
            computed_at=computed_at,
            reason="unsupported_horizon",
        )
    if computed_at is None:
        return _unknown_result(
            episode_id=episode_id,
            horizon_id=spec.horizon_id,
            computed_at=None,
            reason="invalid_computed_at",
        )

    as_of_raw = _first_field(observation, episode, names=("as_of_bar_ts",))
    anchor_at = _parse_datetime(as_of_raw)
    if anchor_at is None:
        return _unknown_result(
            episode_id=episode_id,
            horizon_id=spec.horizon_id,
            computed_at=computed_at,
            reason="invalid_as_of_bar_ts",
        )

    interval = _text(_first_field(observation, episode, names=("bar_interval", "interval")))
    interval_duration = parse_bar_interval(interval)
    if interval_duration is None:
        return _unknown_result(
            episode_id=episode_id,
            horizon_id=spec.horizon_id,
            computed_at=computed_at,
            reason="invalid_source_interval",
            anchor_at=anchor_at,
        )
    anchor_raw = _first_field(observation, episode, names=("anchor", "anchor_bar"))
    if anchor_raw is None:
        return _unknown_result(
            episode_id=episode_id,
            horizon_id=spec.horizon_id,
            computed_at=computed_at,
            reason="missing_anchor_bar",
            anchor_at=anchor_at,
            max_lateness=interval_duration,
        )

    anchor = _evidence_from(
        anchor_raw,
        fallback_ts=anchor_at,
        fallback_source="unknown",
        fallback_interval=interval,
        fallback_semantics="bar_close",
        fallback_price_basis=None,
    )
    if not isinstance(anchor, _BarEvidence) or not _valid_price(anchor.close):
        return _unknown_result(
            episode_id=episode_id,
            horizon_id=spec.horizon_id,
            computed_at=computed_at,
            reason="invalid_anchor_bar",
            anchor_at=anchor_at,
            max_lateness=interval_duration,
        )

    anchor_end_at = anchor.end_at
    if interval_duration > spec.duration:
        # A daily-only observation cannot honestly emit a 4h elapsed label.
        # Treating its next session bar as T+4h would turn calendar closure
        # into a silent cross-horizon fallback.
        return _unknown_result(
            episode_id=episode_id,
            horizon_id=spec.horizon_id,
            computed_at=computed_at,
            reason="source_interval_exceeds_horizon",
            anchor=anchor,
            anchor_at=anchor_at,
            anchor_end_at=anchor_end_at,
            target_at=anchor_end_at + spec.duration,
            max_lateness=interval_duration,
        )

    # ``as_of_bar_ts`` remains the immutable observation identity.  For a
    # provider that stamps bar starts, however, the market transition cannot
    # start until that bar has closed.  ``anchor_end_at`` is therefore the
    # causal clock for every endpoint and path metric below.
    target_at = anchor_end_at + spec.duration
    deadline_at = target_at + interval_duration
    evidence_bars, invalid_bar_count = _normalize_bars(
        bars,
        fallback_source=anchor.source,
        fallback_interval=interval,
        fallback_semantics=anchor.timestamp_semantics,
        fallback_price_basis=anchor.price_basis,
    )
    compatible_bars = [bar for bar in evidence_bars if _same_market_series(anchor, bar)]
    window_bars = [bar for bar in compatible_bars if target_at <= bar.end_at <= deadline_at]
    available_endpoints = [bar for bar in window_bars if bar.end_at <= bar.available_at <= computed_at]
    endpoint = min(
        available_endpoints,
        key=lambda bar: (bar.end_at, bar.available_at, bar.evidence_id),
        default=None,
    )

    if endpoint is None:
        if evidence_bars and not compatible_bars:
            return _unknown_result(
                episode_id=episode_id,
                horizon_id=spec.horizon_id,
                computed_at=computed_at,
                reason="no_compatible_market_series",
                anchor=anchor,
                anchor_at=anchor_at,
                anchor_end_at=anchor_end_at,
                target_at=target_at,
                max_lateness=interval_duration,
            )
        if invalid_bar_count and not evidence_bars:
            return _unknown_result(
                episode_id=episode_id,
                horizon_id=spec.horizon_id,
                computed_at=computed_at,
                reason="invalid_bar_evidence",
                anchor=anchor,
                anchor_at=anchor_at,
                anchor_end_at=anchor_end_at,
                target_at=target_at,
                max_lateness=interval_duration,
            )
        status = "pending" if computed_at < deadline_at else "missing"
        return _base_result(
            episode_id=episode_id,
            horizon_id=spec.horizon_id,
            status=status,
            computed_at=computed_at,
            anchor=anchor,
            anchor_at=anchor_at,
            anchor_end_at=anchor_end_at,
            target_at=target_at,
            deadline_at=deadline_at,
            max_lateness=interval_duration,
            reason=("endpoint_not_yet_available" if status == "pending" else "endpoint_missing_within_lateness"),
        )

    if not _valid_price(endpoint.close):
        return _unknown_result(
            episode_id=episode_id,
            horizon_id=spec.horizon_id,
            computed_at=computed_at,
            reason="invalid_target_close",
            anchor=anchor,
            anchor_at=anchor_at,
            anchor_end_at=anchor_end_at,
            target_at=target_at,
            deadline_at=deadline_at,
            max_lateness=interval_duration,
            endpoint=endpoint,
        )

    simple_return = endpoint.close / anchor.close - 1.0
    log_return = math.log(endpoint.close / anchor.close)
    direction = _direction(simple_return)
    mfe, mae, path_bar_count = _path_extremes(
        anchor,
        compatible_bars,
        anchor_end_at=anchor_end_at,
        endpoint=endpoint,
    )
    episode_training_eligible = _field(episode, "training_eligible") is True
    training_eligible = bool(
        episode_training_eligible
        and endpoint.available_at <= computed_at
        and endpoint.available_at >= endpoint.end_at
        and endpoint.availability_provenance == "explicit"
        and endpoint.source != "unknown"
    )
    result = _base_result(
        episode_id=episode_id,
        horizon_id=spec.horizon_id,
        status="observed",
        computed_at=computed_at,
        anchor=anchor,
        anchor_at=anchor_at,
        anchor_end_at=anchor_end_at,
        target_at=target_at,
        deadline_at=deadline_at,
        max_lateness=interval_duration,
        endpoint=endpoint,
        reason="observed_endpoint",
    )
    result.update(
        {
            "actual_elapsed_seconds": _seconds(endpoint.end_at - anchor_end_at),
            "lag_seconds": _seconds(endpoint.end_at - target_at),
            "simple_return": simple_return,
            "log_return": log_return,
            "direction": direction,
            "direction_band": DIRECTION_BAND,
            "direction_semantics_version": DIRECTION_SEMANTICS_VERSION,
            "mfe": mfe,
            "mae": mae,
            "path_bar_count": path_bar_count,
            "training_eligible": training_eligible,
            "training_eligibility_reason": (
                "episode_and_endpoint_causally_eligible"
                if training_eligible
                else _ineligibility_reason(episode_training_eligible, endpoint)
            ),
        }
    )
    return _with_event_id(result)


def _base_result(
    *,
    episode_id: str,
    horizon_id: str,
    status: str,
    computed_at: datetime,
    anchor: _BarEvidence | None,
    anchor_at: datetime,
    anchor_end_at: datetime,
    target_at: datetime,
    deadline_at: datetime,
    max_lateness: timedelta,
    endpoint: _BarEvidence | None = None,
    reason: str,
) -> dict[str, object]:
    """Return the complete JSON-safe envelope shared by every known outcome."""

    result: dict[str, object] = {
        "schema_version": WORLD_OUTCOME_SCHEMA_VERSION,
        "label_semantics_version": LABEL_SEMANTICS_VERSION,
        "episode_id": episode_id,
        "event_type": _event_type(status),
        "horizon_id": horizon_id,
        "horizon": _horizon_payload(horizon_id, max_lateness),
        "status": status,
        "as_of_bar_ts": _iso(anchor_at),
        "anchor_end_at": _iso(anchor_end_at),
        "target_at": _iso(target_at),
        "deadline_at": _iso(deadline_at),
        "max_lateness_seconds": _seconds(max_lateness),
        "anchor_bar": anchor.to_dict() if anchor is not None else None,
        "anchor_evidence_id": anchor.evidence_id if anchor is not None else None,
        "target_bar": endpoint.to_dict() if endpoint is not None else None,
        "target_evidence_id": endpoint.evidence_id if endpoint is not None else None,
        "anchor_close": anchor.close if anchor is not None else None,
        "endpoint_close": endpoint.close if endpoint is not None else None,
        "endpoint_bar_ts": _iso(endpoint.end_at) if endpoint is not None else None,
        "source": endpoint.source if endpoint is not None else None,
        "source_raw_sha256": endpoint.fingerprint if endpoint is not None else None,
        "availability_provenance": endpoint.availability_provenance if endpoint is not None else None,
        "available_at": _iso(endpoint.available_at) if endpoint is not None else None,
        # Explicit alias makes causal-cutoff consumers independent of UI wording.
        "label_available_at": _iso(endpoint.available_at) if endpoint is not None else None,
        "computed_at": _iso(computed_at),
        "actual_elapsed_seconds": None,
        "lag_seconds": None,
        "simple_return": None,
        "log_return": None,
        "direction": None,
        "direction_band": DIRECTION_BAND,
        "direction_semantics_version": DIRECTION_SEMANTICS_VERSION,
        "mfe": None,
        "mae": None,
        "path_bar_count": 0,
        "training_eligible": False,
        "training_eligibility_reason": "not_observed",
        "reason": reason,
    }
    return _with_event_id(result)


def _unknown_result(
    *,
    episode_id: str,
    horizon_id: str,
    computed_at: datetime | None,
    reason: str,
    anchor: _BarEvidence | None = None,
    anchor_at: datetime | None = None,
    anchor_end_at: datetime | None = None,
    target_at: datetime | None = None,
    deadline_at: datetime | None = None,
    max_lateness: timedelta | None = None,
    endpoint: _BarEvidence | None = None,
) -> dict[str, object]:
    """Produce a fail-closed unknown outcome without inventing values."""

    result: dict[str, object] = {
        "schema_version": WORLD_OUTCOME_SCHEMA_VERSION,
        "label_semantics_version": LABEL_SEMANTICS_VERSION,
        "episode_id": episode_id,
        "event_type": "outcome_unavailable",
        "horizon_id": horizon_id,
        "horizon": _horizon_payload(horizon_id, max_lateness),
        "status": "unknown",
        "as_of_bar_ts": _iso(anchor_at) if anchor_at is not None else None,
        "anchor_end_at": _iso(anchor_end_at) if anchor_end_at is not None else None,
        "target_at": _iso(target_at) if target_at is not None else None,
        "deadline_at": _iso(deadline_at) if deadline_at is not None else None,
        "max_lateness_seconds": _seconds(max_lateness) if max_lateness is not None else None,
        "anchor_bar": anchor.to_dict() if anchor is not None else None,
        "anchor_evidence_id": anchor.evidence_id if anchor is not None else None,
        "target_bar": endpoint.to_dict() if endpoint is not None else None,
        "target_evidence_id": endpoint.evidence_id if endpoint is not None else None,
        "anchor_close": anchor.close if anchor is not None else None,
        "endpoint_close": endpoint.close if endpoint is not None else None,
        "endpoint_bar_ts": _iso(endpoint.end_at) if endpoint is not None else None,
        "source": endpoint.source if endpoint is not None else None,
        "source_raw_sha256": endpoint.fingerprint if endpoint is not None else None,
        "availability_provenance": endpoint.availability_provenance if endpoint is not None else None,
        "available_at": _iso(endpoint.available_at) if endpoint is not None else None,
        "label_available_at": _iso(endpoint.available_at) if endpoint is not None else None,
        "computed_at": _iso(computed_at) if computed_at is not None else None,
        "actual_elapsed_seconds": None,
        "lag_seconds": None,
        "simple_return": None,
        "log_return": None,
        "direction": None,
        "direction_band": DIRECTION_BAND,
        "direction_semantics_version": DIRECTION_SEMANTICS_VERSION,
        "mfe": None,
        "mae": None,
        "path_bar_count": 0,
        "training_eligible": False,
        "training_eligibility_reason": "unknown_outcome",
        "reason": reason,
    }
    return _with_event_id(result)


def _coerce_horizon(value: object) -> HorizonSpec | None:
    """Accept a horizon id or the duck-typed domain ``OutcomeHorizon``."""

    horizon_id = _horizon_id(value)
    spec = _HORIZONS_BY_ID.get(horizon_id)
    if spec is None:
        return None
    raw_seconds = _field(value, "duration_seconds")
    if raw_seconds is None:
        raw_duration = _field(value, "duration")
        if isinstance(raw_duration, timedelta):
            raw_seconds = raw_duration.total_seconds()
    if raw_seconds is None:
        return spec
    try:
        seconds = float(raw_seconds)
    except (TypeError, ValueError):
        return None
    return spec if math.isclose(seconds, spec.duration.total_seconds()) else None


def _horizon_id(value: object) -> str:
    if isinstance(value, str):
        return value
    return _text(_field(value, "horizon_id")) or "unknown"


def _normalize_bars(
    bars: Iterable[object],
    *,
    fallback_source: str,
    fallback_interval: str | None,
    fallback_semantics: str,
    fallback_price_basis: str | None,
) -> tuple[list[_BarEvidence], int]:
    normalized: list[_BarEvidence] = []
    invalid = 0
    for raw in bars:
        evidence = _evidence_from(
            raw,
            fallback_source=fallback_source,
            fallback_interval=fallback_interval,
            fallback_semantics=fallback_semantics,
            fallback_price_basis=fallback_price_basis,
        )
        if evidence is _SKIPPED_NON_BAR:
            continue
        if evidence is None:
            invalid += 1
            continue
        normalized.append(evidence)
    normalized.sort(key=lambda bar: (bar.end_at, bar.available_at, bar.evidence_id))
    return normalized, invalid


def _evidence_from(
    raw: object,
    *,
    fallback_ts: datetime | None = None,
    fallback_source: str,
    fallback_interval: str | None,
    fallback_semantics: str,
    fallback_price_basis: str | None,
) -> _BarEvidence | object | None:
    """Normalize a Bar, mapping, or domain AnchorBar without mutating it."""

    ts = _parse_datetime(_first_field(raw, names=("ts", "timestamp", "bar_ts"))) or fallback_ts
    if ts is None:
        return None
    source = _text(_first_field(raw, names=("source", "data_source"))) or fallback_source
    interval = _text(_first_field(raw, names=("interval", "bar_interval"))) or fallback_interval
    semantics = _text(_first_field(raw, names=("timestamp_semantics",))) or fallback_semantics or "unknown"
    price_basis = _text(_first_field(raw, names=("price_basis",))) or fallback_price_basis
    explicit_end = _parse_datetime(_first_field(raw, names=("bar_end_at", "end_at", "completed_at")))
    end_at = explicit_end or completed_bar_cutoff(
        as_of_bar_ts=ts,
        timestamp_semantics=semantics,
        bar_interval=interval,
    )
    explicit_available_at = _parse_datetime(
        _first_field(raw, names=("available_at", "captured_at", "observed_at", "ingested_at"))
    )
    if end_at is None:
        # An unknown timestamp convention cannot prove a fully completed bar.
        return None
    if not is_eligible_completed_bar(
        ts=ts,
        bar_interval=interval,
        timestamp_semantics=semantics,
        end_at=end_at,
        available_at=explicit_available_at,
    ):
        return _SKIPPED_NON_BAR
    availability_provenance = "explicit" if explicit_available_at is not None else "inferred_from_bar_end"
    available_at = explicit_available_at or end_at
    fingerprint_payload = {
        "ts": _iso(ts),
        "bar_end_at": _iso(end_at),
        "open": _finite_number(_field(raw, "open")),
        "high": _finite_number(_field(raw, "high")),
        "low": _finite_number(_field(raw, "low")),
        "close": _finite_number(_field(raw, "close")),
        "volume": _finite_number(_field(raw, "volume")),
        "source": source,
        "interval": interval,
        "timestamp_semantics": semantics,
        "price_basis": price_basis,
    }
    fingerprint = _sha256(fingerprint_payload)
    return _BarEvidence(
        ts=ts,
        end_at=end_at,
        available_at=available_at,
        open=fingerprint_payload["open"],
        high=fingerprint_payload["high"],
        low=fingerprint_payload["low"],
        close=fingerprint_payload["close"],
        volume=fingerprint_payload["volume"],
        source=source,
        interval=interval,
        timestamp_semantics=semantics,
        price_basis=price_basis,
        availability_provenance=availability_provenance,
        fingerprint=fingerprint,
        evidence_id=f"market_bar:{fingerprint}",
    )


def _same_market_series(anchor: _BarEvidence, candidate: _BarEvidence) -> bool:
    if anchor.source != "unknown" and candidate.source != "unknown" and anchor.source != candidate.source:
        return False
    if (
        anchor.price_basis is not None
        and candidate.price_basis is not None
        and anchor.price_basis != candidate.price_basis
    ):
        return False
    if anchor.interval is not None and candidate.interval is not None and anchor.interval != candidate.interval:
        return False
    return True


def _path_extremes(
    anchor: _BarEvidence,
    bars: Sequence[_BarEvidence],
    *,
    anchor_end_at: datetime,
    endpoint: _BarEvidence,
) -> tuple[float | None, float | None, int]:
    """Return MFE/MAE on complete path bars without using post-endpoint data."""

    path = [anchor]
    path.extend(
        bar
        for bar in bars
        if anchor_end_at <= bar.end_at <= endpoint.end_at and bar.available_at <= endpoint.available_at
    )
    deduped = {bar.evidence_id: bar for bar in path}
    ordered = [deduped[key] for key in sorted(deduped)]
    highs = [bar.high for bar in ordered if _valid_price(bar.high)]
    lows = [bar.low for bar in ordered if _valid_price(bar.low)]
    mfe = max(highs) / anchor.close - 1.0 if highs else None
    mae = min(lows) / anchor.close - 1.0 if lows else None
    return mfe, mae, len(ordered)


def _direction(simple_return: float) -> str:
    if simple_return >= DIRECTION_BAND:
        return "UP"
    if simple_return <= -DIRECTION_BAND:
        return "DOWN"
    return "FLAT"


def _ineligibility_reason(episode_training_eligible: bool, endpoint: _BarEvidence) -> str:
    if not episode_training_eligible:
        return "episode_not_training_eligible"
    if endpoint.availability_provenance != "explicit":
        return "target_available_at_inferred"
    if endpoint.source == "unknown":
        return "target_source_unknown"
    return "target_not_causally_available"


def _episode_id(episode: object, observation: object) -> str:
    explicit = _text(_field(episode, "episode_id"))
    if explicit:
        return explicit
    # Fallback intentionally considers observation-only fields.  In particular,
    # it ignores action, decision, broker, PnL, queue, and scheduler fields.
    anchor = _first_field(observation, episode, names=("anchor", "anchor_bar"))
    as_of = _first_field(observation, episode, names=("as_of_bar_ts",))
    payload = {
        "symbol": _text(_first_field(observation, episode, names=("symbol",))),
        "venue": _text(_first_field(observation, episode, names=("venue",))),
        "bar_interval": _text(_first_field(observation, episode, names=("bar_interval", "interval"))),
        "as_of_bar_ts": _iso(_parse_datetime(as_of)) if _parse_datetime(as_of) is not None else None,
        "anchor": _observation_bar_identity(anchor),
        "feature_contract_version": _text(_first_field(observation, episode, names=("feature_contract_version",))),
        "sampling_policy_version": _text(_first_field(observation, episode, names=("sampling_policy_version",))),
    }
    return f"world_episode:{_sha256(payload)}"


def _observation_bar_identity(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    return {
        "ts": _iso(_parse_datetime(_first_field(raw, names=("ts", "timestamp", "bar_ts"))))
        if _parse_datetime(_first_field(raw, names=("ts", "timestamp", "bar_ts"))) is not None
        else None,
        "open": _finite_number(_field(raw, "open")),
        "high": _finite_number(_field(raw, "high")),
        "low": _finite_number(_field(raw, "low")),
        "close": _finite_number(_field(raw, "close")),
        "volume": _finite_number(_field(raw, "volume")),
        "source": _text(_field(raw, "source")),
        "timestamp_semantics": _text(_field(raw, "timestamp_semantics")),
    }


def _with_event_id(result: dict[str, object]) -> dict[str, object]:
    outcome_id = _sha256(
        {
            "episode_id": result["episode_id"],
            "horizon_id": result["horizon_id"],
            "label_semantics_version": result["label_semantics_version"],
        }
    )
    result["outcome_id"] = f"world_outcome:{outcome_id}"
    source_raw_sha256 = result.get("source_raw_sha256")
    event_id = world_outcome_event_id(
        episode_id=str(result["episode_id"]),
        horizon_id=str(result["horizon_id"]),
        status=str(result["status"]),
        source_raw_sha256=(
            str(source_raw_sha256) if isinstance(source_raw_sha256, str) and source_raw_sha256 else None
        ),
    )
    result["event_id"] = event_id
    result["outcome_event_id"] = event_id
    return result


def _horizon_payload(horizon_id: str, max_lateness: timedelta | None) -> dict[str, object] | None:
    spec = _HORIZONS_BY_ID.get(horizon_id)
    if spec is None:
        return None
    return {
        "horizon_id": spec.horizon_id,
        "duration_seconds": int(spec.duration.total_seconds()),
        "endpoint_rule": "first_fully_available_bar_at_or_after_target.v1",
        "max_lateness_seconds": (int(max_lateness.total_seconds()) if max_lateness is not None else None),
    }


def _event_type(status: str) -> str:
    if status == "observed":
        return "outcome_observed"
    if status == "pending":
        return "outcome_scheduled"
    return "outcome_unavailable"


def _first_field(*objects: object, names: tuple[str, ...]) -> object:
    for obj in objects:
        for name in names:
            value = _field(obj, name)
            if value is not None:
                return value
    return None


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None) if value is not None else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    # The shared WorldEpisode contract rejects naïve timestamps.  Keeping the
    # same rule here avoids silently relabelling exchange-local/daily data as
    # UTC and makes an unavailable timestamp an explicit ``unknown`` outcome.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_price(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def _seconds(value: timedelta) -> float:
    return value.total_seconds()


def _sha256(value: object) -> str:
    return canonical_sha256(value)


__all__ = [
    "DEFAULT_HORIZONS",
    "DIRECTION_BAND",
    "DIRECTION_SEMANTICS_VERSION",
    "ELAPSED_1D",
    "ELAPSED_4H",
    "HorizonSpec",
    "LABEL_SEMANTICS_VERSION",
    "label_episode",
    "label_horizon",
]
