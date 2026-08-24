"""Typed DBnomics series and Yahoo commodity adapters for source-only macro facts."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

import yaml

from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.domain.world_macro import (
    MACRO_FACT_KINDS,
    MACRO_FEATURE_VALUES,
    MacroDerivationPolicy,
    MacroNumericValue,
    MacroScope,
    MacroSourceFact,
    MacroSourceRegistry,
    MacroSourceRegistryEntry,
)
from trader.domain.world_scope import WorldScopeMapping
from trader.infrastructure.market_sources.commodity_prices import parse_yahoo_last_close
from trader.infrastructure.market_sources.macro_series import parse_dbnomics_last_observation


_METRIC_UNITS = MappingProxyType(
    {
        "policy_rate": "percent",
        "cpi_index": "index",
        "unemployment_rate": "percent",
        "brent_front_month_usd": "usd",
        "gold_front_month_usd": "usd",
    }
)
_YAHOO_UA = "Mozilla/5.0 (compatible; casys-trader/1.0)"


class MacroSourceFetchError(Exception):
    """Provider fetch ended as explicit missingness, never a filled macro value."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class MacroHttpResponse:
    status: int
    body: str | bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class MacroHttpTransport(Protocol):
    def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> MacroHttpResponse: ...


class UrllibMacroTransport:
    """Object-shaped stdlib adapter. Production default for MacroHttpTransport."""

    def get(self, url: str, *, timeout_s: float, headers: dict[str, str]) -> MacroHttpResponse:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                return MacroHttpResponse(
                    status=int(response.status),
                    body=response.read(),
                    headers={str(key): str(value) for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            header_map = {}
            if exc.headers is not None:
                header_map = {str(key): str(value) for key, value in exc.headers.items()}
            return MacroHttpResponse(status=int(exc.code), body=exc.read(), headers=header_map)
        except TimeoutError:
            raise
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError) or "timed out" in str(exc).lower():
                raise TimeoutError("timeout") from exc
            raise


@dataclass(frozen=True)
class ProviderBudget:
    provider_id: str
    base: str
    cooldown_h: float
    timeout_s: float
    min_interval_s: float
    observations: int | None = None
    range: str | None = None
    interval: str | None = None


@dataclass(frozen=True)
class MacroCollectionBudgets:
    worker: str
    fetch_in_run_cycle: bool
    fetch_in_world_capture_worker: bool
    max_parallel_requests: int
    retry_max: int
    honor_retry_after: bool
    on_429_or_timeout: str
    providers: Mapping[str, ProviderBudget]


@dataclass(frozen=True)
class MacroTtlPolicy:
    series_point_daily_h: int
    series_point_monthly_d: int
    market_benchmark_daily_h: int


@dataclass(frozen=True)
class WorldMacroOperatorBundle:
    registry: MacroSourceRegistry
    registry_operator_config_sha256: str
    policy: MacroDerivationPolicy
    policy_content_sha256: str
    scope_mapping: WorldScopeMapping
    scope_operator_config_sha256: str
    declared_gaps: tuple[str, ...]
    excluded_providers: tuple[str, ...]
    budgets: MacroCollectionBudgets
    ttl: MacroTtlPolicy


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must be a mapping")
    return payload


def _as_text_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, list | tuple):
        raise TypeError("operator list must be a sequence of strings")
    return tuple(str(item) for item in value)


def _require_operator_hash(payload: Mapping[str, Any], field_name: str) -> tuple[dict[str, Any], str]:
    if field_name not in payload or not payload[field_name]:
        raise ValueError(f"{field_name} is required")
    claimed = str(payload[field_name])
    hashed = dict(payload)
    hashed.pop(field_name)
    digest = canonical_sha256(hashed)
    if digest != claimed:
        raise ValueError(f"{field_name} does not match the canonical operator config")
    return hashed, claimed


def load_world_macro_operator_configs(*, config_dir: Path) -> WorldMacroOperatorBundle:
    """Load and hash-check the committed operator gates. No provider values are invented."""

    sources_path = Path(config_dir) / "world_macro_sources.yaml"
    policy_path = Path(config_dir) / "world_macro_derivation_policy.yaml"
    mapping_path = Path(config_dir) / "world_scope_mapping.yaml"
    sources = _load_yaml_mapping(sources_path)
    policy_raw = _load_yaml_mapping(policy_path)
    mapping_raw = _load_yaml_mapping(mapping_path)

    registry = MacroSourceRegistry.from_mapping(
        {
            "schema_version": sources.get("schema_version"),
            "registry_version": sources.get("registry_version"),
            "entries": sources.get("entries") or (),
            "content_sha256": sources.get("content_sha256"),
        }
    )
    _, registry_operator_hash = _require_operator_hash(sources, "operator_config_sha256")

    _, policy_hash = _require_operator_hash(policy_raw, "content_sha256")
    vocabularies = policy_raw.get("vocabularies") or {}
    if not isinstance(vocabularies, Mapping):
        raise TypeError("policy vocabularies must be a mapping")
    for dimension, values in vocabularies.items():
        if frozenset(values) != MACRO_FEATURE_VALUES[str(dimension)]:
            raise ValueError(f"policy vocabulary mismatch for {dimension}")
    policy = MacroDerivationPolicy(
        transform_version=policy_raw.get("transform_version"),
        producer_version=policy_raw.get("producer_version"),
    )
    if sources.get("authority") != "shadow_only" or sources.get("decision_effect") != "none":
        raise ValueError("registry authority must stay shadow_only with decision_effect none")
    if policy_raw.get("authority") != "shadow_only" or policy_raw.get("decision_effect") != "none":
        raise ValueError("policy authority must stay shadow_only with decision_effect none")

    scope_mapping = WorldScopeMapping.from_mapping(
        {
            "schema_version": mapping_raw.get("schema_version"),
            "mapping_id": mapping_raw.get("mapping_id"),
            "entries": mapping_raw.get("entries") or (),
            "content_sha256": mapping_raw.get("content_sha256"),
        }
    )
    _, scope_operator_hash = _require_operator_hash(mapping_raw, "operator_config_sha256")

    excluded = _as_text_tuple(sources.get("excluded_providers"))
    declared_gaps = _as_text_tuple(sources.get("declared_gaps"))
    source_ids = {entry.source_id for entry in registry.entries}
    if source_ids.intersection(declared_gaps):
        raise ValueError("declared gaps must not appear as registry entries")
    for entry in registry.entries:
        if entry.provider_id in excluded:
            raise ValueError("registry entry uses an excluded provider")
        if entry.fact_kind not in MACRO_FACT_KINDS:
            raise ValueError("registry entry fact_kind is not admitted")

    budgets_raw = sources.get("budgets") or {}
    if not isinstance(budgets_raw, Mapping):
        raise TypeError("budgets must be a mapping")
    if budgets_raw.get("fetch_in_run_cycle") is not False:
        raise ValueError("fetch_in_run_cycle must be false")
    if budgets_raw.get("fetch_in_world_capture_worker") is not False:
        raise ValueError("fetch_in_world_capture_worker must be false")
    providers_raw = budgets_raw.get("providers") or {}
    if not isinstance(providers_raw, Mapping):
        raise TypeError("provider budgets must be a mapping")
    providers: dict[str, ProviderBudget] = {}
    for provider_id, raw in providers_raw.items():
        if not isinstance(raw, Mapping):
            raise TypeError("provider budget must be a mapping")
        providers[str(provider_id)] = ProviderBudget(
            provider_id=str(provider_id),
            base=str(raw["base"]),
            cooldown_h=float(raw["cooldown_h"]),
            timeout_s=float(raw["timeout_s"]),
            min_interval_s=float(raw["min_interval_s"]),
            observations=None if raw.get("observations") is None else int(raw["observations"]),
            range=None if raw.get("range") is None else str(raw["range"]),
            interval=None if raw.get("interval") is None else str(raw["interval"]),
        )
    budgets = MacroCollectionBudgets(
        worker=str(budgets_raw.get("worker") or ""),
        fetch_in_run_cycle=False,
        fetch_in_world_capture_worker=False,
        max_parallel_requests=int(budgets_raw.get("max_parallel_requests") or 1),
        retry_max=int(budgets_raw.get("retry_max") or 0),
        honor_retry_after=bool(budgets_raw.get("honor_retry_after")),
        on_429_or_timeout=str(budgets_raw.get("on_429_or_timeout") or "missing"),
        providers=MappingProxyType(providers),
    )
    ttl_raw = sources.get("ttl") or {}
    if not isinstance(ttl_raw, Mapping):
        raise TypeError("ttl must be a mapping")
    ttl = MacroTtlPolicy(
        series_point_daily_h=int(ttl_raw["series_point_daily_h"]),
        series_point_monthly_d=int(ttl_raw["series_point_monthly_d"]),
        market_benchmark_daily_h=int(ttl_raw["market_benchmark_daily_h"]),
    )
    return WorldMacroOperatorBundle(
        registry=registry,
        registry_operator_config_sha256=registry_operator_hash,
        policy=policy,
        policy_content_sha256=policy_hash,
        scope_mapping=scope_mapping,
        scope_operator_config_sha256=scope_operator_hash,
        declared_gaps=declared_gaps,
        excluded_providers=excluded,
        budgets=budgets,
        ttl=ttl,
    )


def _decode_body(body: str | bytes) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8")
    return body


def _period_occurred_at(period: str) -> datetime:
    text = period.strip()
    if len(text) == 10:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    if len(text) == 7:
        return datetime.fromisoformat(f"{text}-01").replace(tzinfo=timezone.utc)
    raise ValueError(f"unsupported period: {period}")


def _valid_until(observed_at: datetime, *, fact_kind: str, period: str, ttl: MacroTtlPolicy) -> datetime:
    if fact_kind == "market_benchmark":
        return observed_at + timedelta(hours=ttl.market_benchmark_daily_h)
    if len(period) == 7:
        return observed_at + timedelta(days=ttl.series_point_monthly_d)
    return observed_at + timedelta(hours=ttl.series_point_daily_h)


def _retry_after_seconds(response: MacroHttpResponse, fallback: float) -> float:
    raw = None
    for key, value in response.headers.items():
        if str(key).lower() == "retry-after":
            raw = str(value).strip()
            break
    if not raw:
        return fallback
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return fallback


def urllib_macro_transport(url: str, *, timeout_s: float, headers: dict[str, str]) -> MacroHttpResponse:
    """Compatibility wrapper. Production wiring uses UrllibMacroTransport.get()."""

    return UrllibMacroTransport().get(url, timeout_s=timeout_s, headers=headers)


class ProviderRateLimiter:
    """Single-flight, coalescent GET with min-interval, Retry-After, and one retry max."""

    def __init__(
        self,
        *,
        min_interval_s: float,
        retry_max: int,
        honor_retry_after: bool,
        timeout_s: float,
        transport: MacroHttpTransport,
        clock: Callable[[], datetime],
        sleeper: Callable[[float], None],
    ) -> None:
        self._min_interval_s = min_interval_s
        self._retry_max = retry_max
        self._honor_retry_after = honor_retry_after
        self._timeout_s = timeout_s
        self._transport = transport
        self._clock = clock
        self._sleeper = sleeper
        self._gate = threading.Lock()
        self._map_lock = threading.Lock()
        self._inflight: dict[str, Future[MacroHttpResponse]] = {}
        self._last_request_at: datetime | None = None

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> MacroHttpResponse:
        request_headers = headers or {}
        owner = False
        with self._map_lock:
            future = self._inflight.get(url)
            if future is None:
                future = Future()
                self._inflight[url] = future
                owner = True
        if not owner:
            try:
                return future.result(timeout=self._timeout_s)
            except TimeoutError as exc:
                raise MacroSourceFetchError("timeout", "timeout") from exc
        try:
            with self._gate:
                self._respect_min_interval()
                response = self._get_with_retry(url, request_headers)
                self._last_request_at = self._clock()
            future.set_result(response)
            return response
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._map_lock:
                current = self._inflight.get(url)
                if current is future:
                    self._inflight.pop(url, None)

    def _respect_min_interval(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = (self._clock() - self._last_request_at).total_seconds()
        remaining = self._min_interval_s - elapsed
        if remaining > 0:
            self._sleeper(remaining)

    def _get_with_retry(self, url: str, headers: dict[str, str]) -> MacroHttpResponse:
        max_attempts = 1 + max(0, self._retry_max)
        last_timeout: TimeoutError | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._transport.get(url, timeout_s=self._timeout_s, headers=headers)
            except TimeoutError as exc:
                last_timeout = TimeoutError("timeout")
                last_timeout.__cause__ = exc
                if attempt >= max_attempts:
                    raise MacroSourceFetchError("timeout", "timeout") from last_timeout
                self._sleeper(self._min_interval_s)
                continue
            if response.status == 429:
                if attempt >= max_attempts:
                    raise MacroSourceFetchError("http_429", "HTTP 429")
                delay = (
                    _retry_after_seconds(response, self._min_interval_s)
                    if self._honor_retry_after
                    else self._min_interval_s
                )
                self._sleeper(min(delay, self._timeout_s))
                continue
            if response.status >= 400:
                raise MacroSourceFetchError("unavailable", f"HTTP {response.status}")
            return response
        if last_timeout is not None:
            raise MacroSourceFetchError("timeout", "timeout") from last_timeout
        raise MacroSourceFetchError("unavailable", "unavailable")


def _parse_dbnomics(body: str) -> tuple[str, float, datetime | None] | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    obs = parse_dbnomics_last_observation(payload)
    if obs is None:
        return None
    period, value = obs
    published: datetime | None = None
    try:
        docs = payload["series"]["docs"]
        indexed = docs[0].get("indexed_at") if docs else None
        if indexed:
            published = parse_utc_timestamp(indexed, "indexed_at")
    except (KeyError, IndexError, TypeError, ValueError):
        published = None
    return period, float(value), published


def _series_url(entry: MacroSourceRegistryEntry, budget: ProviderBudget) -> str:
    if entry.provider_id == "dbnomics":
        observations = 1 if budget.observations is None else budget.observations
        return f"{budget.base.rstrip('/')}/series/{entry.provider_entity_id}?observations={observations}"
    range_value = budget.range or "5d"
    interval = budget.interval or "1d"
    return f"{budget.base}{entry.provider_entity_id}?range={range_value}&interval={interval}"


class BoundMacroSourceAdapter:
    """One registry entry as a MacroSourcePort. Requested run scope is not rewritten."""

    def __init__(
        self,
        *,
        entry: MacroSourceRegistryEntry,
        limiter: ProviderRateLimiter,
        budget: ProviderBudget,
        ttl: MacroTtlPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._entry = entry
        self._limiter = limiter
        self._budget = budget
        self._ttl = ttl
        self._clock = clock
        self._cache_lock = threading.Lock()
        self._cache: tuple[datetime, tuple[MacroSourceFact, ...]] | None = None
        self._last_leaf: MacroSourceFact | None = None

    def read_facts(self, scope: MacroScope, observed_at: datetime) -> tuple[MacroSourceFact, ...]:
        del scope
        now = self._clock()
        with self._cache_lock:
            if self._cache is not None:
                cached_at, facts = self._cache
                if (now - cached_at).total_seconds() < self._budget.cooldown_h * 3600.0:
                    return facts
        url = _series_url(self._entry, self._budget)
        headers = {"User-Agent": _YAHOO_UA} if self._entry.provider_id == "yahoo_finance" else {}
        response = self._limiter.get(url, headers=headers)
        body = _decode_body(response.body)
        parsed = self._parse(body)
        if parsed is None:
            raise MacroSourceFetchError("unavailable", "unavailable")
        period, value, published_at = parsed
        observed = _utc(observed_at)
        fact = self._to_fact(period, value, published_at, observed, url)
        with self._cache_lock:
            if self._last_leaf is not None and self._last_leaf.fact_key == fact.fact_key:
                if self._last_leaf.fact_version_id == fact.fact_version_id:
                    facts = (self._last_leaf,)
                else:
                    facts = (
                        self._last_leaf.corrected(
                            value=fact.value,
                            published_at=fact.published_at,
                            ingested_at=fact.ingested_at,
                        ),
                    )
            else:
                facts = (fact,)
            self._last_leaf = facts[0]
            self._cache = (now, facts)
            return facts

    def _parse(self, body: str) -> tuple[str, float, datetime | None] | None:
        if self._entry.provider_id == "dbnomics":
            return _parse_dbnomics(body)
        obs = parse_yahoo_last_close(body)
        if obs is None:
            return None
        period, value = obs
        return period, float(value), None

    def _to_fact(
        self,
        period: str,
        value: float,
        published_at: datetime | None,
        observed_at: datetime,
        url: str,
    ) -> MacroSourceFact:
        unit = _METRIC_UNITS.get(self._entry.metric_key)
        if unit is None:
            raise ValueError(f"no closed unit for metric_key {self._entry.metric_key}")
        occurred_at = _period_occurred_at(period)
        published = published_at if published_at is not None else occurred_at
        return MacroSourceFact(
            fact_kind=self._entry.fact_kind,
            metric_key=self._entry.metric_key,
            scope=self._entry.canonical_scope,
            value=MacroNumericValue(number=value, unit=unit),
            period=period,
            occurred_at=occurred_at,
            published_at=published,
            ingested_at=observed_at,
            source={
                "provider_id": self._entry.provider_id,
                "adapter_version": self._entry.adapter_version,
                "source_record_id": f"{self._entry.provider_entity_id}:{period}",
                "source_ref": url,
            },
            valid_until=_valid_until(
                observed_at,
                fact_kind=self._entry.fact_kind,
                period=period,
                ttl=self._ttl,
            ),
        )


class DBnomicsSeriesAdapter(BoundMacroSourceAdapter):
    def __init__(self, **kwargs: Any) -> None:
        entry: MacroSourceRegistryEntry = kwargs["entry"]
        if entry.provider_id != "dbnomics":
            raise ValueError("DBnomicsSeriesAdapter requires provider_id dbnomics")
        super().__init__(**kwargs)


class YahooCommodityAdapter(BoundMacroSourceAdapter):
    def __init__(self, **kwargs: Any) -> None:
        entry: MacroSourceRegistryEntry = kwargs["entry"]
        if entry.provider_id != "yahoo_finance":
            raise ValueError("YahooCommodityAdapter requires provider_id yahoo_finance")
        super().__init__(**kwargs)


def build_macro_source_ports(
    bundle: WorldMacroOperatorBundle,
    *,
    transport: MacroHttpTransport,
    clock: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, BoundMacroSourceAdapter]:
    resolved_clock = clock or (lambda: datetime.now(timezone.utc))
    resolved_sleeper = sleeper or time.sleep
    limiters: dict[str, ProviderRateLimiter] = {}
    for provider_id, budget in bundle.budgets.providers.items():
        limiters[provider_id] = ProviderRateLimiter(
            min_interval_s=budget.min_interval_s,
            retry_max=bundle.budgets.retry_max,
            honor_retry_after=bundle.budgets.honor_retry_after,
            timeout_s=budget.timeout_s,
            transport=transport,
            clock=resolved_clock,
            sleeper=resolved_sleeper,
        )
    ports: dict[str, BoundMacroSourceAdapter] = {}
    adapters = {"dbnomics": DBnomicsSeriesAdapter, "yahoo_finance": YahooCommodityAdapter}
    for entry in bundle.registry.entries:
        budget = bundle.budgets.providers[entry.provider_id]
        adapter_cls = adapters[entry.provider_id]
        ports[entry.source_id] = adapter_cls(
            entry=entry,
            limiter=limiters[entry.provider_id],
            budget=budget,
            ttl=bundle.ttl,
            clock=resolved_clock,
        )
    return ports


__all__ = [
    "BoundMacroSourceAdapter",
    "DBnomicsSeriesAdapter",
    "MacroCollectionBudgets",
    "MacroHttpResponse",
    "MacroSourceFetchError",
    "MacroTtlPolicy",
    "ProviderBudget",
    "ProviderRateLimiter",
    "UrllibMacroTransport",
    "WorldMacroOperatorBundle",
    "YahooCommodityAdapter",
    "build_macro_source_ports",
    "load_world_macro_operator_configs",
    "urllib_macro_transport",
]
