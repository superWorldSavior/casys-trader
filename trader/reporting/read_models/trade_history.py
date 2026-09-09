"""Canonical trade-history reconstruction and regime filtering read model."""

from __future__ import annotations

import gzip
import json
import math
import zlib
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path

from trader.application.execute.fee_estimate import (
    UNAVAILABLE_COMMISSION_MODELS,
)
from trader.domain.execution.fill_accounting import POSITION_EPSILON
from trader.domain.market import fx

_UNKNOWN_EXPERIMENT_COHORT = "unknown"
_MIXED_EXPERIMENT_COHORT = "mixed"
_US_COMMISSION_MODELS = frozenset(
    {
        "ibkr_us_stock_tiered",
        "ibkr_us_fractional_stock",
        "ibkr_us_stock_tiered_mixed_fractional",
    }
)
_EUROPE_COMMISSION_SUFFIXES = frozenset(
    {".AS", ".BR", ".DE", ".HE", ".LS", ".MI", ".PA", ".VI"}
)
_NORDIC_COMMISSION_SUFFIXES = frozenset({".CO", ".OL", ".ST"})
_TAIWAN_COMMISSION_SUFFIXES = frozenset({".T", ".TW", ".TWO"})


def _read_perf_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "model_performance.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _read_decision_rows_by_id(
    state_dir: Path,
    *,
    decision_ids: frozenset[str],
) -> tuple[dict[str, list[dict]], dict[str, object]]:
    """Index archived and live audit ledgers for causal experiment metadata."""

    if not decision_ids:
        return {}, {"status": "available", "reason": None, "source": None}
    path = state_dir / "decisions.jsonl"
    rows_by_id: dict[str, list[dict]] = defaultdict(list)
    archive_dir = state_dir / "archive"
    sources: list[tuple[Path, bool]] = []
    if archive_dir.exists():
        sources.extend(
            (archive_path, True)
            for archive_path in sorted(
                archive_dir.glob("decisions-????-??.jsonl.gz")
            )
        )
    if path.exists():
        sources.append((path, False))

    for source, compressed in sources:
        try:
            opener = gzip.open if compressed else source.open
            if compressed:
                handle_context = opener(source, "rt", encoding="utf-8")
            else:
                handle_context = opener("r", encoding="utf-8")
            with handle_context as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        return {}, {
                            "status": "unavailable",
                            "reason": "decision_ledger_malformed",
                            "source": str(source),
                        }
                    if not isinstance(row, dict):
                        return {}, {
                            "status": "unavailable",
                            "reason": "decision_ledger_malformed",
                            "source": str(source),
                        }
                    decision_id = str(row.get("decision_id") or "").strip()
                    if decision_id in decision_ids:
                        rows_by_id[decision_id].append(row)
        except (
            EOFError,
            OSError,
            UnicodeDecodeError,
            gzip.BadGzipFile,
            zlib.error,
        ) as exc:
            return {}, {
                "status": "unavailable",
                "reason": "decision_ledger_unreadable",
                "source": str(source),
                "detail": type(exc).__name__,
            }
    return rows_by_id, {
        "status": "available",
        "reason": None,
        "source": None,
    }


def _unknown_experiment_attribution(
    reason: str,
    *,
    quality: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "experiment_cohort": _UNKNOWN_EXPERIMENT_COHORT,
        "experiment_id": None,
        "experiment_ids": [],
        "experiment_attribution_status": _UNKNOWN_EXPERIMENT_COHORT,
        "experiment_attribution_reason": reason,
        "experiment_attribution_quality": dict(
            quality
            or {"status": "available", "reason": None, "source": None}
        ),
        "experiment_components": None,
        "provider": "unknown",
        "model": "unknown",
        "profile": None,
    }


def _validated_decision_experiment(
    row: Mapping[str, object],
) -> dict[str, object] | None:
    """Revalidate the persisted v1/v2 hash and its provider/model projection."""

    from trader.support.metadata.experiment import inherited_experiment

    status = row.get("experiment_status")
    if not isinstance(status, Mapping):
        return None
    descriptor = inherited_experiment(
        {
            "schema_version": status.get("schema_version"),
            "experiment_id": row.get("experiment_id"),
            "components": row.get("experiment_components"),
            "decision_grade": status.get("decision_grade") is True,
        }
    )
    if descriptor is None:
        return None
    components = descriptor.get("components")
    model_components = (
        components.get("model") if isinstance(components, Mapping) else None
    )
    if not isinstance(model_components, Mapping):
        return None
    provider = str(model_components.get("provider") or "").strip()
    model = str(model_components.get("model") or "").strip()
    row_provider = str(row.get("llm_provider") or "").strip()
    row_model = str(row.get("llm_model") or "").strip()
    if (
        not provider
        or not model
        or (row_provider and row_provider != provider)
        or (row_model and row_model != model)
    ):
        return None
    return {
        **descriptor,
        "provider": provider,
        "model": model,
    }


def _descriptor_signature(descriptor: Mapping[str, object]) -> str:
    """Canonical signature preventing arbitrary merge of one experiment ID."""

    relevant = {
        "schema_version": descriptor.get("schema_version"),
        "experiment_id": descriptor.get("experiment_id"),
        "components": descriptor.get("components"),
        "decision_grade": descriptor.get("decision_grade"),
        "provider": descriptor.get("provider"),
        "model": descriptor.get("model"),
    }
    try:
        return json.dumps(
            relevant,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return "invalid"


def _cycle_experiment_attribution(
    cycle: Mapping[str, object],
    *,
    decisions_by_id: Mapping[str, list[dict]],
) -> dict[str, object]:
    raw_ids = cycle.get("entry_decision_ids")
    candidates = raw_ids if isinstance(raw_ids, list) else []
    if not candidates and cycle.get("entry_decision_id"):
        candidates = [cycle["entry_decision_id"]]
    decision_ids = list(
        dict.fromkeys(decision_id for candidate in candidates if (decision_id := str(candidate or "").strip()))
    )

    base = _unknown_experiment_attribution("missing_entry_decision_id")
    if not decision_ids:
        return base

    decisions: list[dict[str, object]] = []
    for decision_id in decision_ids:
        matching = decisions_by_id.get(decision_id, [])
        if not matching:
            return {
                **base,
                "experiment_attribution_reason": "missing_decision",
            }
        descriptors: list[dict[str, object]] = []
        for row in matching:
            descriptor = _validated_decision_experiment(row)
            if descriptor is None:
                return {
                    **base,
                    "experiment_attribution_reason": (
                        "invalid_experiment_descriptor"
                    ),
                }
            descriptors.append(descriptor)
        signatures = {_descriptor_signature(value) for value in descriptors}
        if len(signatures) != 1 or "invalid" in signatures:
            return {
                **base,
                "experiment_attribution_reason": "conflicting_decision_rows",
            }
        decisions.append(descriptors[0])

    experiment_ids: list[str] = []
    for decision in decisions:
        decision_grade = decision.get("decision_grade") is True
        experiment_id = str(decision.get("experiment_id") or "").strip()
        if not decision_grade:
            return {
                **base,
                "experiment_attribution_reason": "not_decision_grade",
            }
        if not experiment_id:
            return {
                **base,
                "experiment_attribution_reason": "invalid_experiment_descriptor",
            }
        experiment_ids.append(experiment_id)

    unique_experiment_ids = sorted(set(experiment_ids))
    if len(unique_experiment_ids) != 1:
        return {
            **base,
            "experiment_cohort": _MIXED_EXPERIMENT_COHORT,
            "experiment_ids": unique_experiment_ids,
            "experiment_attribution_status": _MIXED_EXPERIMENT_COHORT,
            "experiment_attribution_reason": "mixed_experiment_ids",
        }

    if len({_descriptor_signature(decision) for decision in decisions}) != 1:
        return {
            **base,
            "experiment_ids": unique_experiment_ids,
            "experiment_attribution_reason": "inconsistent_experiment_metadata",
        }

    experiment_id = unique_experiment_ids[0]
    components = decisions[0].get("components")
    components = dict(components) if isinstance(components, Mapping) else None
    model_components = components.get("model") if isinstance(components, Mapping) else None
    model_components = dict(model_components) if isinstance(model_components, Mapping) else {}
    execution_profile = model_components.get("execution_profile")
    profile = {
        "preset": model_components.get("preset"),
        "execution_profile": (dict(execution_profile) if isinstance(execution_profile, Mapping) else None),
    }
    return {
        "experiment_cohort": experiment_id,
        "experiment_id": experiment_id,
        "experiment_ids": unique_experiment_ids,
        "experiment_attribution_status": "attributed",
        "experiment_attribution_reason": None,
        "experiment_attribution_quality": {
            "status": "available",
            "reason": None,
            "source": None,
        },
        "experiment_components": components,
        "provider": str(decisions[0]["provider"]),
        "model": str(decisions[0]["model"]),
        "profile": profile,
    }


def attribute_position_cycles_to_experiments(
    state_dir: Path,
    cycles: Iterable[Mapping[str, object]],
) -> list[dict]:
    """Attach fail-closed experiment cohorts to canonical completed cycles.

    Economic fields stay untouched.  ``decisions.jsonl`` contributes only the
    causal experiment descriptor reached through canonical fill decision IDs.
    A cycle is attributed only when every entry decision exists, is marked
    decision-grade, and resolves to the same experiment ID.
    """

    cycle_rows = [dict(cycle) for cycle in cycles]
    decision_ids: set[str] = set()
    for cycle in cycle_rows:
        raw_ids = cycle.get("entry_decision_ids")
        candidates = raw_ids if isinstance(raw_ids, list) else []
        if not candidates and cycle.get("entry_decision_id"):
            candidates = [cycle["entry_decision_id"]]
        decision_ids.update(decision_id for candidate in candidates if (decision_id := str(candidate or "").strip()))
    decisions_by_id, attribution_quality = _read_decision_rows_by_id(
        state_dir,
        decision_ids=frozenset(decision_ids),
    )
    if attribution_quality["status"] != "available":
        return [
            {
                **dict(cycle),
                **_unknown_experiment_attribution(
                    "attribution_unavailable",
                    quality=attribution_quality,
                ),
            }
            for cycle in cycle_rows
        ]
    return [
        {
            **dict(cycle),
            **_cycle_experiment_attribution(
                cycle,
                decisions_by_id=decisions_by_id,
            ),
        }
        for cycle in cycle_rows
    ]


def _read_canonical_fill_rows(state_dir: Path) -> list[dict] | None:
    """Read the durable broker ledger when one exists.

    ``None`` means that this is a legacy state directory with no broker
    ledger.  An existing (even empty) broker ledger remains authoritative and
    must never fall back to the secondary model-performance projection.
    """

    db_path = state_dir / "casys.db"
    if db_path.exists():
        from trader.infrastructure.state_db.broker_store import SqliteBroker
        from trader.infrastructure.state_db.connection import open_state_db

        return SqliteBroker(open_state_db(db_path)).fills()

    broker_path = state_dir / "broker.json"
    if broker_path.exists():
        raw = json.loads(broker_path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("broker JSON invalide")
        fills = raw.get("fills", [])
        if not isinstance(fills, list):
            raise ValueError("broker fills invalides")
        if any(not isinstance(row, Mapping) for row in fills):
            raise ValueError("canonical fill row invalide: objet attendu")
        return [dict(row) for row in fills]

    return None


def _match_number(value: object) -> float | None:
    parsed = _finite_float(value)
    return None if parsed is None else round(parsed, 12)


def _fill_match_key(row: Mapping[str, object]) -> tuple[object, ...]:
    """Return the immutable execution identity shared by both ledgers."""

    return (
        str(row.get("symbol") or ""),
        str(row.get("side") or row.get("action") or "").upper(),
        _match_number(row.get("quantity")),
        _match_number(row.get("price")),
        str(row.get("ts") or ""),
    )


_PERFORMANCE_ENRICHMENT_FIELDS = (
    "decision_id",
    "confidence",
    "intent",
    "exit_reason",
    "source_plan_id",
    "llm_provider",
    "llm_model",
    "llm_fallback_reason",
)


def _enrich_canonical_fills(
    fills: Iterable[Mapping[str, object]],
    performance_rows: Iterable[Mapping[str, object]],
) -> list[dict]:
    """Enrich canonical fills without letting the projection change economics.

    Matching is one-to-one.  Missing, duplicated or divergent JSONL rows can
    therefore only remove optional attribution metadata; they cannot create a
    fill or override side, quantity, price, timestamp, commission or FX.
    """

    projections: dict[tuple[object, ...], deque[Mapping[str, object]]] = defaultdict(deque)
    for row in performance_rows:
        projections[_fill_match_key(row)].append(row)
    canonical_counts = Counter(_fill_match_key(fill) for fill in fills)

    rows: list[dict] = []
    for fill in fills:
        row = dict(fill)
        row["action"] = str(row.get("side") or row.get("action") or "").upper()
        key = _fill_match_key(row)
        candidates = projections.get(key)
        # Enrichment is advisory.  One-to-many or many-to-one identities are
        # ambiguous, so no JSONL metadata is selected by ordering accident.
        projection = (
            candidates[0]
            if candidates is not None
            and len(candidates) == 1
            and canonical_counts[key] == 1
            else None
        )
        if projection is not None:
            for field in _PERFORMANCE_ENRICHMENT_FIELDS:
                if field not in row or row[field] is None:
                    row[field] = projection.get(field)
        rows.append(row)
    return rows


def _holding_minutes(entry_ts: str, exit_ts: str) -> float | None:
    try:
        entry = datetime.fromisoformat(entry_ts)
        exit_ = datetime.fromisoformat(exit_ts)
    except (OverflowError, TypeError, ValueError):
        return None
    try:
        return (exit_ - entry).total_seconds() / 60.0
    except TypeError:
        return None


class _OpenLeg:
    """Signed open position with averaged entry metadata."""

    def __init__(self) -> None:
        self.qty = 0.0
        self.avg_price = 0.0
        self.entry_notional_usd = 0.0
        self.commission_usd = 0.0
        self.commission_available = True
        self.unavailable_commission_models: set[str] = set()
        self.commission_unavailable_reasons: set[str] = set()
        self.entry_ts: str | None = None
        self._conf_sum = 0.0
        self._conf_qty = 0.0
        self.entry_decision_ids: list[str] = []
        self.position_cycle_id: str | None = None

    @property
    def entry_confidence(self) -> float | None:
        return self._conf_sum / self._conf_qty if self._conf_qty else None

    def open_or_add(
        self,
        *,
        added_qty: float,
        price: float,
        ts: str,
        confidence: float | None,
        commission_usd: float = 0.0,
        commission_available: bool = True,
        commission_model: str = "none",
        commission_unavailable_reason: str | None = None,
        fill_fx_rate: float = 1.0,
        decision_id: str | None = None,
        position_cycle_id: str | None = None,
    ) -> None:
        if self.qty == 0.0:
            self.entry_ts = ts
            self._conf_sum = 0.0
            self._conf_qty = 0.0
            self.entry_notional_usd = 0.0
            self.commission_usd = 0.0
            self.commission_available = True
            self.unavailable_commission_models = set()
            self.commission_unavailable_reasons = set()
            self.entry_decision_ids = []
            self.position_cycle_id = position_cycle_id
        new_qty = self.qty + added_qty
        weighted_native_cost = (
            self.avg_price * abs(self.qty) + price * abs(added_qty)
        )
        entry_notional_increment = price * abs(added_qty) * fill_fx_rate
        new_entry_notional = self.entry_notional_usd + entry_notional_increment
        new_commission = self.commission_usd + max(commission_usd, 0.0)
        if (
            not math.isfinite(new_qty)
            or new_qty == 0.0
            or not math.isfinite(weighted_native_cost)
            or not math.isfinite(entry_notional_increment)
            or not math.isfinite(new_entry_notional)
            or not math.isfinite(new_commission)
        ):
            raise ValueError("canonical fill economics overflow")
        avg_price = weighted_native_cost / abs(new_qty)
        if not math.isfinite(avg_price):
            raise ValueError("canonical fill average price overflow")
        self.avg_price = avg_price
        self.qty = new_qty
        self.entry_notional_usd = new_entry_notional
        self.commission_usd = new_commission
        if not commission_available:
            self.commission_available = False
            self.unavailable_commission_models.add(commission_model)
            if commission_unavailable_reason:
                self.commission_unavailable_reasons.add(
                    commission_unavailable_reason
                )
        if confidence is not None:
            self._conf_sum += confidence * abs(added_qty)
            self._conf_qty += abs(added_qty)
        if decision_id and decision_id not in self.entry_decision_ids:
            self.entry_decision_ids.append(decision_id)

    def scale_entry_weight(self, factor: float) -> None:
        self._conf_sum *= factor
        self._conf_qty *= factor


def _commission_amount(value: object, *, strict: bool) -> float:
    if value is None:
        return 0.0
    parsed = _finite_float(value)
    if parsed is None or parsed < 0.0:
        if strict:
            raise ValueError(f"canonical fill commission invalide: {value!r}")
        return 0.0
    return parsed


def _commission_to_usd(
    *,
    symbol: str,
    amount: object,
    currency: object,
    fill_fx_rate: object,
    strict: bool,
) -> float:
    """Convert one fill commission without borrowing another fill's FX rate."""

    parsed_amount = _commission_amount(amount, strict=strict)
    if parsed_amount == 0.0:
        return 0.0

    commission_currency = str(currency or fx.BASE_CCY).strip().upper()
    if commission_currency == fx.BASE_CCY:
        return parsed_amount

    quote_currency = fx.currency_for(symbol).upper()
    if commission_currency != quote_currency:
        raise ValueError(
            "commission currency non resolue: "
            f"symbol={symbol!r} quote={quote_currency!r} "
            f"commission={commission_currency!r}"
        )

    parsed_rate = _finite_float(fill_fx_rate)
    if parsed_rate is None or parsed_rate <= 0.0:
        raise ValueError(
            f"commission fx rate invalide: symbol={symbol!r} currency={commission_currency!r} rate={fill_fx_rate!r}"
        )
    converted = parsed_amount * parsed_rate
    if not math.isfinite(converted):
        raise ValueError(
            "commission USD non finie: "
            f"symbol={symbol!r} amount={parsed_amount!r} rate={parsed_rate!r}"
        )
    return converted


def _expected_commission_contract(
    symbol: str,
) -> tuple[frozenset[str], str] | None:
    normalized_symbol = symbol.upper()
    suffix = fx.mapped_suffix_for(normalized_symbol)
    if normalized_symbol.endswith("=X"):
        return frozenset({"ibkr_spot_fx_tiered"}), "USD"
    if normalized_symbol == "^FCHI":
        return frozenset({"ibkr_france40_cfd"}), "EUR"
    if normalized_symbol.startswith("^"):
        return frozenset({"ibkr_index_cfd"}), "USD"
    if suffix in _EUROPE_COMMISSION_SUFFIXES:
        return frozenset({"ibkr_europe_stock_tiered"}), fx.currency_for(symbol)
    if suffix == ".MC":
        return frozenset(
            {
                "ibkr_spain_stock_fixed_smartrouting",
                "ibkr_spain_stock_fixed_smartrouting_fractional",
            }
        ), fx.currency_for(symbol)
    if suffix == ".L":
        return frozenset({"ibkr_uk_stock_tiered"}), fx.currency_for(symbol)
    if suffix == ".SW":
        return frozenset({"ibkr_switzerland_stock_tiered"}), fx.currency_for(symbol)
    if suffix in _NORDIC_COMMISSION_SUFFIXES:
        return frozenset({"ibkr_nordic_stock_tiered"}), fx.currency_for(symbol)
    if suffix in _TAIWAN_COMMISSION_SUFFIXES:
        return frozenset({"ibkr_taiwan_stock_tiered"}), fx.currency_for(symbol)
    if suffix is None and fx.currency_for(symbol).upper() == "USD":
        return _US_COMMISSION_MODELS, "USD"
    return None


def _commission_quality(
    *,
    symbol: str,
    amount: object,
    model: object,
    currency: object,
    strict_canonical: bool,
) -> tuple[bool, str, str | None]:
    normalized = str(model or "none").strip().lower() or "none"
    if amount is None:
        return False, normalized, "commission_not_recorded"
    parsed_amount = _finite_float(amount)
    if parsed_amount is None or parsed_amount < 0.0:
        return False, normalized, "commission_amount_invalid"
    if normalized == "none":
        return False, normalized, "commission_not_modeled"
    if normalized in UNAVAILABLE_COMMISSION_MODELS:
        return False, normalized, "commission_model_unavailable"
    expected = _expected_commission_contract(symbol)
    if expected is None:
        return False, normalized, "commission_venue_unpriced"
    expected_models, expected_currency = expected
    if not isinstance(currency, str) or not currency.strip():
        return False, normalized, "commission_currency_missing"
    normalized_currency = str(currency or "USD").strip().upper() or "USD"
    if normalized not in expected_models:
        return False, normalized, "commission_model_incompatible"
    if normalized_currency != expected_currency.upper():
        return False, normalized, "commission_currency_incompatible"
    return True, normalized, None


def _fill_fx_rate(
    *,
    symbol: str,
    value: object,
    allow_legacy_default: bool,
) -> float:
    parsed_rate = _finite_float(value)
    if parsed_rate is not None and parsed_rate > 0.0:
        return parsed_rate
    if not allow_legacy_default:
        raise ValueError(
            "fill fx rate manquant ou invalide: "
            f"symbol={symbol!r} rate={value!r}"
        )
    return 1.0


def compute_round_trips(
    state_dir: Path | None = None,
    *,
    fills: Iterable[Mapping[str, object]] | None = None,
    require_canonical: bool = False,
) -> list[dict]:
    """Reconstruct realised exit legs from the canonical broker fill ledger.

    A partial exit is intentionally emitted immediately: callers that analyse
    exit mechanisms need one observation per realised leg.  Headline outcome
    metrics must collapse these rows with :func:`aggregate_position_cycles`.

    ``model_performance.jsonl`` is only a best-effort metadata projection.  It
    is used as a legacy source when no broker ledger exists at all.
    """

    if fills is None:
        if state_dir is None:
            raise ValueError("state_dir requis sans fills explicites")
        performance_rows = _read_perf_rows(state_dir)
        canonical_fills = _read_canonical_fill_rows(state_dir)
        if require_canonical and canonical_fills is None:
            return []
    else:
        if require_canonical:
            raise ValueError(
                "require_canonical incompatible avec fills explicites"
            )
        # ``fills`` without a state directory is intrinsically pure.  A caller
        # that also supplies ``state_dir`` opts into the same exact-match,
        # metadata-only enrichment used by live canonical read models.
        performance_rows = _read_perf_rows(state_dir) if state_dir is not None else []
        canonical_fills = list(fills)
    if canonical_fills is not None and any(
        not isinstance(row, Mapping) for row in canonical_fills
    ):
        raise ValueError("canonical fill row invalide: objet attendu")
    rows = performance_rows if canonical_fills is None else _enrich_canonical_fills(canonical_fills, performance_rows)
    using_legacy_projection = canonical_fills is None
    strict_canonical = not using_legacy_projection
    rows.sort(key=lambda row: (str(row.get("symbol")), str(row.get("ts"))))
    legs: dict[str, _OpenLeg] = {}
    cycle_numbers: dict[str, int] = {}
    trips: list[dict] = []

    for index, row in enumerate(rows):
        raw_symbol = row.get("symbol")
        symbol = str(raw_symbol or "").strip()
        action = str(row.get("action", "")).upper()
        if not symbol or not isinstance(raw_symbol, str):
            if strict_canonical:
                raise ValueError(
                    f"canonical fill index={index}: symbole invalide"
                )
            continue
        if action not in ("BUY", "SELL"):
            if strict_canonical:
                raise ValueError(
                    f"canonical fill index={index}: side invalide {action!r}"
                )
            continue
        raw_qty = _finite_float(row.get("quantity"))
        price = _finite_float(row.get("price"))
        if raw_qty is None or price is None or raw_qty == 0.0 or price <= 0.0:
            if strict_canonical:
                raise ValueError(
                    f"canonical fill index={index}: quantité/prix invalide"
                )
            continue
        if raw_qty < 0.0 and strict_canonical:
            raise ValueError(
                f"canonical fill index={index}: quantité négative"
            )
        qty = abs(raw_qty)

        ts = str(row.get("ts") or "").strip()
        try:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            if strict_canonical:
                raise ValueError(
                    f"canonical fill index={index}: timestamp invalide"
                ) from None
            continue

        confidence = _finite_float(row.get("confidence"))
        row_fx_rate = _fill_fx_rate(
            symbol=symbol,
            value=row.get("fx_rate"),
            allow_legacy_default=using_legacy_projection,
        )
        row_commission_available, row_commission_model, row_commission_reason = (
            _commission_quality(
                symbol=symbol,
                amount=row.get("commission"),
                model=row.get("commission_model"),
                currency=row.get("commission_currency"),
                strict_canonical=strict_canonical,
            )
        )
        if row_commission_available:
            row_commission_usd = _commission_to_usd(
                symbol=symbol,
                amount=row.get("commission"),
                currency=row.get("commission_currency"),
                fill_fx_rate=row_fx_rate,
                strict=strict_canonical,
            )
        else:
            _commission_amount(
                row.get("commission"),
                strict=strict_canonical,
            )
            row_commission_usd = 0.0
        row_decision_id = str(row.get("decision_id") or "").strip() or None
        signed = qty if action == "BUY" else -qty
        leg = legs.setdefault(symbol, _OpenLeg())

        # The broker considers a position under POSITION_EPSILON flat.  Keep a
        # raw same-side residual only long enough to attach an opposite dust
        # cleanup fill (and its commission) to the cycle that just closed.
        # Any material fill after that broker-flat boundary starts fresh.
        broker_flat = abs(leg.qty) <= POSITION_EPSILON
        dust_cleanup = (
            0.0 < abs(leg.qty) <= POSITION_EPSILON
            and qty <= POSITION_EPSILON
            and (leg.qty > 0) != (signed > 0)
        )
        position_cycle_id: str | None = None
        opens_position = False
        if broker_flat and not dust_cleanup:
            legs[symbol] = _OpenLeg()
            leg = legs[symbol]
            # A dust fill on a broker-flat position has no position cycle to
            # attribute.  It must not become a phantom long or short.
            if qty <= POSITION_EPSILON:
                continue
            cycle_numbers[symbol] = cycle_numbers.get(symbol, 0) + 1
            position_cycle_id = f"{symbol}:{cycle_numbers[symbol]}"
            opens_position = True
        elif (leg.qty > 0) == (signed > 0):
            opens_position = True

        if opens_position:
            leg.open_or_add(
                added_qty=signed,
                price=price,
                ts=ts,
                confidence=confidence,
                commission_usd=row_commission_usd,
                commission_available=row_commission_available,
                commission_model=row_commission_model,
                commission_unavailable_reason=row_commission_reason,
                fill_fx_rate=row_fx_rate,
                decision_id=row_decision_id,
                position_cycle_id=position_cycle_id,
            )
            continue

        closing_qty = min(qty, abs(leg.qty))
        entry_sign = 1.0 if leg.qty > 0 else -1.0
        old_abs = abs(leg.qty)
        entry_commission = leg.commission_usd * (closing_qty / old_abs) if old_abs > 0 else 0.0
        exit_commission = (
            row_commission_usd
            if dust_cleanup
            else row_commission_usd * (closing_qty / qty) if qty > 0 else 0.0
        )
        entry_notional_usd = leg.entry_notional_usd * (closing_qty / old_abs) if old_abs > 0 else 0.0
        exit_notional_usd = price * closing_qty * row_fx_rate
        gross_pnl_usd = entry_sign * (exit_notional_usd - entry_notional_usd)
        total_commission_usd = entry_commission + exit_commission
        net_pnl_usd = gross_pnl_usd - total_commission_usd
        if not all(
            math.isfinite(value)
            for value in (
                entry_commission,
                exit_commission,
                entry_notional_usd,
                exit_notional_usd,
                gross_pnl_usd,
                total_commission_usd,
                net_pnl_usd,
            )
        ):
            raise ValueError("canonical position-cycle economics overflow")
        commission_available = (
            leg.commission_available and row_commission_available
        )
        unavailable_commission_models = set(
            leg.unavailable_commission_models
        )
        commission_unavailable_reasons = set(
            leg.commission_unavailable_reasons
        )
        if not row_commission_available:
            unavailable_commission_models.add(row_commission_model)
            if row_commission_reason:
                commission_unavailable_reasons.add(row_commission_reason)
        commission_quality = {
            "status": (
                "available" if commission_available else "unavailable"
            ),
            "reason": (
                None
                if commission_available
                else (
                    next(iter(commission_unavailable_reasons))
                    if len(commission_unavailable_reasons) == 1
                    else "multiple_commission_quality_failures"
                )
            ),
            "models": sorted(unavailable_commission_models),
            "reasons": sorted(commission_unavailable_reasons),
        }
        trips.append(
            {
                "symbol": symbol,
                "side": "LONG" if leg.qty > 0 else "SHORT",
                "quantity": closing_qty,
                "entry_price": leg.avg_price,
                "exit_price": price,
                "gross_pnl": gross_pnl_usd,
                "entry_notional_usd": entry_notional_usd,
                "commission": (
                    total_commission_usd if commission_available else None
                ),
                "pnl": net_pnl_usd if commission_available else None,
                "commission_quality": commission_quality,
                "entry_ts": leg.entry_ts,
                "exit_ts": ts,
                "holding_minutes": _holding_minutes(leg.entry_ts or ts, ts),
                "entry_confidence": leg.entry_confidence,
                "exit_reason": row.get("exit_reason") or str(row.get("intent") or ""),
                "source_plan_id": row.get("source_plan_id"),
                "entry_decision_id": (leg.entry_decision_ids[0] if len(leg.entry_decision_ids) == 1 else None),
                "entry_decision_ids": list(leg.entry_decision_ids),
                "position_cycle_id": leg.position_cycle_id,
                "position_cycle_closed": closing_qty >= old_abs - POSITION_EPSILON,
            }
        )

        remaining = qty - closing_qty
        leg.qty += signed
        if remaining > POSITION_EPSILON:
            fresh = _OpenLeg()
            cycle_numbers[symbol] = cycle_numbers.get(symbol, 0) + 1
            fresh.open_or_add(
                added_qty=entry_sign * -remaining,
                price=price,
                ts=ts,
                confidence=confidence,
                commission_usd=max(row_commission_usd - exit_commission, 0.0),
                commission_available=row_commission_available,
                commission_model=row_commission_model,
                commission_unavailable_reason=row_commission_reason,
                fill_fx_rate=row_fx_rate,
                decision_id=row_decision_id,
                position_cycle_id=f"{symbol}:{cycle_numbers[symbol]}",
            )
            legs[symbol] = fresh
        elif leg.qty == 0.0 or (
            abs(leg.qty) <= POSITION_EPSILON
            and (leg.qty > 0) != (entry_sign > 0)
        ):
            legs[symbol] = _OpenLeg()
        else:
            leg.entry_notional_usd = max(
                leg.entry_notional_usd - entry_notional_usd,
                0.0,
            )
            leg.commission_usd = max(
                leg.commission_usd - entry_commission,
                0.0,
            )
            leg.scale_entry_weight(abs(leg.qty) / old_abs)

    return trips


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _unique_text(rows: Iterable[Mapping[str, object]], key: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        raw = row.get(key)
        candidates = raw if isinstance(raw, list) else [raw]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value and value not in values:
                values.append(value)
    return values


def aggregate_position_cycles(
    trips: Iterable[Mapping[str, object]],
) -> list[dict]:
    """Collapse realised exit legs into completed flat-to-flat position cycles.

    Rows carrying a ``position_cycle_id`` are emitted only once the position is
    flat.  Legacy rows without that identifier remain standalone observations,
    preserving backward compatibility with pre-cycle history and unit callers.
    """

    grouped: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    for index, trip in enumerate(trips):
        cycle_id = str(trip.get("position_cycle_id") or "").strip()
        if cycle_id:
            key = ("cycle", str(trip.get("symbol") or ""), cycle_id)
        else:
            key = ("legacy_leg", "", str(index))
        grouped.setdefault(key, []).append(trip)

    cycles: list[dict] = []
    for (kind, _, cycle_key), source_rows in grouped.items():
        if kind == "legacy_leg":
            legacy = dict(source_rows[0])
            legacy.setdefault("position_cycle_closed", True)
            legacy.setdefault("exit_leg_count", 1)
            cycles.append(legacy)
            continue
        if kind == "cycle" and not any(row.get("position_cycle_closed") is True for row in source_rows):
            continue

        rows = sorted(source_rows, key=lambda row: str(row.get("exit_ts") or ""))
        first = rows[0]
        last = rows[-1]
        quantities = [abs(quantity) for row in rows if (quantity := _finite_float(row.get("quantity"))) is not None]
        total_quantity = sum(quantities)
        if not math.isfinite(total_quantity):
            raise ValueError("position-cycle quantity overflow")

        def summed(field: str) -> float:
            total = 0.0
            for row in rows:
                raw = row.get(field)
                if raw is None:
                    continue
                parsed = _finite_float(raw)
                if parsed is None:
                    raise ValueError(
                        f"position-cycle economic field invalide: {field}"
                    )
                total += parsed
                if not math.isfinite(total):
                    raise ValueError(
                        f"position-cycle economic field overflow: {field}"
                    )
            return total

        def quantity_weighted(field: str) -> float | None:
            weighted = 0.0
            weight = 0.0
            for row in rows:
                value = _finite_float(row.get(field))
                quantity = _finite_float(row.get("quantity"))
                if value is None or quantity is None:
                    continue
                row_weight = abs(quantity)
                weighted += value * row_weight
                weight += row_weight
                if not math.isfinite(weighted) or not math.isfinite(weight):
                    raise ValueError(
                        f"position-cycle weighted field overflow: {field}"
                    )
            return weighted / weight if weight > 0.0 else None

        entry_decision_ids: list[str] = []
        for row in rows:
            raw_ids = row.get("entry_decision_ids")
            candidates = raw_ids if isinstance(raw_ids, list) else []
            if not candidates and row.get("entry_decision_id"):
                candidates = [row["entry_decision_id"]]
            for candidate in candidates:
                decision_id = str(candidate or "").strip()
                if decision_id and decision_id not in entry_decision_ids:
                    entry_decision_ids.append(decision_id)

        entry_ts = str(first.get("entry_ts") or "") or None
        exit_ts = str(last.get("exit_ts") or "") or None
        exit_reasons = _unique_text(rows, "exit_reason")
        source_plan_ids = _unique_text(rows, "source_plan_id")
        unavailable_models: set[str] = set()
        unavailable_reasons: set[str] = set()
        commission_available = True
        for row in rows:
            quality = row.get("commission_quality")
            if not isinstance(quality, Mapping):
                commission_available = False
                unavailable_reasons.add("commission_quality_missing")
                continue
            if quality.get("status") == "available":
                continue
            commission_available = False
            models = quality.get("models")
            if isinstance(models, list):
                unavailable_models.update(str(value) for value in models)
            reasons = quality.get("reasons")
            if isinstance(reasons, list):
                unavailable_reasons.update(str(value) for value in reasons)
            elif quality.get("reason"):
                unavailable_reasons.add(str(quality["reason"]))
        cycle_commission_quality = {
            "status": (
                "available" if commission_available else "unavailable"
            ),
            "reason": (
                None
                if commission_available
                else (
                    next(iter(unavailable_reasons))
                    if len(unavailable_reasons) == 1
                    else "multiple_commission_quality_failures"
                )
            ),
            "models": sorted(unavailable_models),
            "reasons": sorted(unavailable_reasons),
        }
        cycles.append(
            {
                "symbol": first.get("symbol"),
                "side": first.get("side"),
                "quantity": total_quantity,
                "entry_price": quantity_weighted("entry_price"),
                "exit_price": quantity_weighted("exit_price"),
                "gross_pnl": summed("gross_pnl"),
                "entry_notional_usd": summed("entry_notional_usd"),
                "commission": (
                    summed("commission") if commission_available else None
                ),
                "pnl": summed("pnl") if commission_available else None,
                "commission_quality": cycle_commission_quality,
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "holding_minutes": (
                    _holding_minutes(entry_ts, exit_ts) if entry_ts is not None and exit_ts is not None else None
                ),
                "entry_confidence": quantity_weighted("entry_confidence"),
                # The final leg names what flattened the position.  All leg
                # mechanisms remain explicit for attribution below.
                "exit_reason": last.get("exit_reason"),
                "exit_reasons": exit_reasons,
                "source_plan_id": (source_plan_ids[0] if len(source_plan_ids) == 1 else None),
                "source_plan_ids": source_plan_ids,
                "entry_decision_id": (entry_decision_ids[0] if len(entry_decision_ids) == 1 else None),
                "entry_decision_ids": entry_decision_ids,
                "position_cycle_id": (cycle_key if kind == "cycle" else first.get("position_cycle_id")),
                "position_cycle_closed": True,
                "exit_leg_count": len(rows),
            }
        )
    return cycles


def _parse_iso_date(value: str, *, field: str) -> date:
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"{field} doit être une date/datetime ISO: {value!r}") from exc


def _normalize_excluded_symbols(
    exclude_symbols: tuple[str, ...] | frozenset[str],
) -> tuple[str, ...]:
    raw_symbols = sorted(exclude_symbols) if isinstance(exclude_symbols, frozenset) else exclude_symbols
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in raw_symbols:
        parsed = str(symbol)
        if parsed in seen:
            continue
        normalized.append(parsed)
        seen.add(parsed)
    return tuple(normalized)


def filter_regime_trips(
    trips: list[dict],
    *,
    since: str | None,
    exclude_symbols: tuple[str, ...] | frozenset[str],
    min_entry_confidence: float | None = None,
) -> tuple[list[dict], dict]:
    """Apply shared date, symbol and entry-confidence attribution filters."""

    since_date = _parse_iso_date(since, field="since") if since is not None else None
    excluded_symbols = _normalize_excluded_symbols(exclude_symbols)
    excluded_symbol_set = set(excluded_symbols)
    kept: list[dict] = []
    n_excluded_trades = 0
    n_excluded_low_confidence = 0

    for trip in trips:
        excluded_regime = False
        if since_date is not None:
            exit_date = _parse_iso_date(
                str(trip.get("exit_ts")),
                field="exit_ts",
            )
            excluded_regime = exit_date < since_date
        if str(trip.get("symbol")) in excluded_symbol_set:
            excluded_regime = True
        if excluded_regime:
            n_excluded_trades += 1
            continue

        entry_confidence = trip.get("entry_confidence")
        if (
            min_entry_confidence is not None
            and entry_confidence is not None
            and entry_confidence < min_entry_confidence
        ):
            n_excluded_low_confidence += 1
            continue
        kept.append(trip)

    return kept, {
        "since": since,
        "excluded_symbols": list(excluded_symbols),
        "n_excluded_trades": n_excluded_trades,
        "min_entry_confidence": min_entry_confidence,
        "n_excluded_low_confidence": n_excluded_low_confidence,
    }


__all__ = [
    "aggregate_position_cycles",
    "compute_round_trips",
    "filter_regime_trips",
]
