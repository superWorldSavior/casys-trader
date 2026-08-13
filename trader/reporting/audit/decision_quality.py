"""Ex-post audit of logged agent decisions."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from trader.domain import decision_reason
from trader.domain.decision_benchmark import (
    BENCHMARK_SEMANTICS_VERSION,
    _MACHINE_REASONS as _MACHINE_REASONS,
    _verdict as _verdict,
    decision_benchmark_context as decision_benchmark_context,
    decision_verdict,
)
from trader.reporting.audit.protocols import PriceHistoryLoader


def parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_horizon(raw: str) -> timedelta:
    value = raw.strip().lower()
    if not value:
        raise ValueError("empty horizon")
    unit = value[-1]
    amount = float(value[:-1])
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    raise ValueError(f"unsupported horizon: {raw}")


def _normalize_prices(items: list[dict]) -> list[tuple[datetime, float]]:
    normalized: list[tuple[datetime, float]] = []
    for item in items:
        ts = parse_ts(item.get("ts"))
        if ts is None:
            continue
        try:
            close = float(item["close"])
        except (KeyError, TypeError, ValueError):
            continue
        normalized.append((ts, close))
    return sorted(normalized, key=lambda pair: pair[0])


def _price_asof(prices: list[tuple[datetime, float]], ts: datetime) -> float | None:
    value: float | None = None
    for price_ts, close in prices:
        if price_ts > ts:
            break
        value = close
    return value


def _price_at_or_after(prices: list[tuple[datetime, float]], ts: datetime) -> float | None:
    for price_ts, close in prices:
        if price_ts >= ts:
            return close
    return None


def decision_commit_key(row: dict) -> str:
    version = row.get("code_version")
    if not isinstance(version, dict):
        return "unknown"
    commit = version.get("git_commit_short") or version.get("git_commit")
    if not commit:
        return "unknown"
    key = str(commit)[:12]
    if version.get("git_dirty"):
        key = f"{key}+dirty"
    return key


def _counter_metrics(counts: Counter) -> dict:
    good = int(counts.get("good", 0))
    bad = int(counts.get("bad", 0))
    neutral = int(counts.get("neutral", 0))
    missed = int(counts.get("missed", 0))
    machine = int(counts.get("machine", 0))
    unknown = int(counts.get("unknown", 0))
    known = good + bad + neutral + missed
    total = known + unknown + machine

    def pct(value: int, denominator: int) -> float | None:
        if denominator <= 0:
            return None
        return round((value / denominator) * 100.0, 2)

    return {
        "total": total,
        "known": known,
        "unknown": unknown,
        "good": good,
        "bad": bad,
        "neutral": neutral,
        "missed": missed,
        "machine": machine,
        "coverage_pct": pct(known, total),
        "good_known_pct": pct(good, known),
        "bad_known_pct": pct(bad, known),
        "neutral_known_pct": pct(neutral, known),
        "missed_known_pct": pct(missed, known),
        "nonbad_known_pct": pct(good + neutral + missed, known),
    }


def summarize_audited_rows(rows: list[dict], horizons: list[str]) -> dict:
    summary: dict[str, Counter] = {horizon: Counter() for horizon in horizons}
    summary_by_commit: dict[str, dict[str, Counter]] = {horizon: {} for horizon in horizons}
    summary_by_basis: dict[str, dict[str, Counter]] = {horizon: {} for horizon in horizons}
    summary_by_reason: dict[str, dict[str, dict[str, Counter]]] = {
        horizon: {} for horizon in horizons
    }
    for row in rows:
        commit_key = decision_commit_key(row)
        row["decision_commit_key"] = commit_key
        reason_code = decision_reason.infer_reason_code(row)
        row["decision_reason_code"] = reason_code
        action = str(row.get("action") or "UNKNOWN").upper()
        audits = row.get("audits")
        if not isinstance(audits, dict):
            continue
        for horizon in horizons:
            entry = audits.get(horizon)
            if not isinstance(entry, dict):
                continue
            verdict = str(entry.get("verdict") or "unknown")
            basis = str(entry.get("benchmark_basis") or "legacy")
            summary[horizon][verdict] += 1
            summary_by_commit[horizon].setdefault(commit_key, Counter())[verdict] += 1
            summary_by_basis[horizon].setdefault(basis, Counter())[verdict] += 1
            reason_by_action = summary_by_reason[horizon].setdefault(action, {})
            reason_by_action.setdefault(reason_code, Counter())[verdict] += 1
    return {
        "summary": {horizon: dict(summary[horizon]) for horizon in horizons},
        "metrics": {
            horizon: _counter_metrics(summary[horizon])
            for horizon in horizons
        },
        "summary_by_commit": {
            horizon: {
                commit: dict(counts)
                for commit, counts in commits.items()
            }
            for horizon, commits in summary_by_commit.items()
        },
        "metrics_by_commit": {
            horizon: {
                commit: _counter_metrics(counts)
                for commit, counts in commits.items()
            }
            for horizon, commits in summary_by_commit.items()
        },
        "summary_by_basis": {
            horizon: {
                basis: dict(counts)
                for basis, counts in bases.items()
            }
            for horizon, bases in summary_by_basis.items()
        },
        "metrics_by_basis": {
            horizon: {
                basis: _counter_metrics(counts)
                for basis, counts in bases.items()
            }
            for horizon, bases in summary_by_basis.items()
        },
        "summary_by_reason": {
            horizon: {
                action: {
                    reason_code: dict(counts)
                    for reason_code, counts in reasons.items()
                }
                for action, reasons in actions.items()
            }
            for horizon, actions in summary_by_reason.items()
        },
        "metrics_by_reason": {
            horizon: {
                action: {
                    reason_code: _counter_metrics(counts)
                    for reason_code, counts in reasons.items()
                }
                for action, reasons in actions.items()
            }
            for horizon, actions in summary_by_reason.items()
        },
    }


def refresh_audit_payload(
    payload: dict,
    *,
    ledger_rows: list[dict] | None = None,
    audit_code_version: dict | None = None,
    include_missing_ledger_rows: bool = False,
) -> dict:
    horizons = [str(horizon) for horizon in payload.get("horizons", [])]
    try:
        threshold_pct = float(payload.get("threshold_pct") or 0.5)
    except (TypeError, ValueError):
        threshold_pct = 0.5
    versions_by_id = {
        row.get("decision_id"): row.get("code_version")
        for row in ledger_rows or []
        if row.get("decision_id") and isinstance(row.get("code_version"), dict)
    }
    rows: list[dict] = []
    seen_ids: set[Any] = set()
    for row in payload.get("rows", []):
        if not isinstance(row, dict):
            continue
        refreshed = dict(row)
        seen_ids.add(refreshed.get("decision_id"))
        version = versions_by_id.get(refreshed.get("decision_id"))
        if isinstance(version, dict):
            refreshed["code_version"] = dict(version)
        refreshed["decision_commit_key"] = decision_commit_key(refreshed)
        audits = refreshed.get("audits")
        if isinstance(audits, dict):
            migrated_audits: dict[str, dict] = {}
            for horizon, raw_entry in audits.items():
                if not isinstance(raw_entry, dict):
                    continue
                entry = dict(raw_entry)
                try:
                    future_return_pct = (
                        None
                        if entry.get("future_return_pct") is None
                        else float(entry["future_return_pct"])
                    )
                except (TypeError, ValueError):
                    future_return_pct = None
                verdict, benchmark_context = decision_verdict(
                    refreshed,
                    future_return_pct,
                    threshold_pct,
                )
                entry.update({"verdict": verdict, **benchmark_context})
                migrated_audits[str(horizon)] = entry
            refreshed["audits"] = migrated_audits
        rows.append(refreshed)
    if include_missing_ledger_rows:
        for ledger_row in ledger_rows or []:
            decision_id = ledger_row.get("decision_id")
            if decision_id in seen_ids:
                continue
            missing = dict(ledger_row)
            missing_audits: dict[str, dict] = {}
            for horizon in horizons:
                verdict, benchmark_context = decision_verdict(missing, None, threshold_pct)
                missing_audits[horizon] = {
                    "entry_price": missing.get("price"),
                    "future_price": None,
                    "future_return_pct": None,
                    "verdict": verdict,
                    **benchmark_context,
                }
            missing["audits"] = missing_audits
            missing["decision_commit_key"] = decision_commit_key(missing)
            rows.append(missing)
            seen_ids.add(decision_id)

    refreshed_payload = dict(payload)
    refreshed_payload["benchmark_semantics_version"] = BENCHMARK_SEMANTICS_VERSION
    refreshed_payload["rows"] = rows
    if audit_code_version is not None:
        refreshed_payload["audit_code_version"] = audit_code_version
    refreshed_payload.update(summarize_audited_rows(rows, horizons))
    return refreshed_payload


def audit_rows(
    rows: list[dict],
    prices_by_symbol: dict[str, list[dict]],
    *,
    horizons: list[str],
    threshold_pct: float,
    audit_code_version: dict | None = None,
) -> dict:
    normalized_prices = {
        symbol: _normalize_prices(prices)
        for symbol, prices in prices_by_symbol.items()
    }
    audited_rows: list[dict] = []

    for row in rows:
        symbol = str(row.get("symbol") or "")
        decision_ts = parse_ts(row.get("cycle_ts"))
        price_series = normalized_prices.get(symbol, [])
        audits: dict[str, dict] = {}
        for horizon in horizons:
            entry_price = row.get("price")
            try:
                entry_price = None if entry_price is None else float(entry_price)
            except (TypeError, ValueError):
                entry_price = None
            if entry_price is None and decision_ts is not None:
                entry_price = _price_asof(price_series, decision_ts)

            future_price = None
            if decision_ts is not None:
                future_price = _price_at_or_after(price_series, decision_ts + parse_horizon(horizon))

            future_return_pct = None
            if entry_price not in {None, 0} and future_price is not None:
                future_return_pct = round(((future_price - entry_price) / entry_price) * 100.0, 6)
            verdict, benchmark_context = decision_verdict(row, future_return_pct, threshold_pct)
            audits[horizon] = {
                "entry_price": entry_price,
                "future_price": future_price,
                "future_return_pct": future_return_pct,
                "verdict": verdict,
                **benchmark_context,
            }

        audited = dict(row)
        audited["decision_commit_key"] = decision_commit_key(row)
        audited["decision_reason_code"] = decision_reason.infer_reason_code(audited)
        audited["audits"] = audits
        audited_rows.append(audited)

    payload = {
        "benchmark_semantics_version": BENCHMARK_SEMANTICS_VERSION,
        "threshold_pct": threshold_pct,
        "horizons": horizons,
        "audit_code_version": audit_code_version,
        "rows": audited_rows,
    }
    return refresh_audit_payload(payload, audit_code_version=audit_code_version)


def load_prices_yfinance(
    symbols: list[str],
    *,
    start: str,
    end: str,
    interval: str,
) -> dict[str, list[dict]]:
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"yfinance unavailable: {exc}") from exc

    prices: dict[str, list[dict]] = {}
    for symbol in symbols:
        df = yf.Ticker(symbol).history(start=start, end=end, interval=interval, auto_adjust=False)
        rows: list[dict] = []
        if df is not None and not df.empty:
            for idx, row in df.iterrows():
                rows.append({"ts": idx.isoformat(), "close": float(row["Close"])})
        prices[symbol] = rows
    return prices


def price_window_for_audit_rows(rows: list[dict], *, horizons: list[str]) -> dict[str, object] | None:
    parsed_times = [parse_ts(row.get("cycle_ts")) for row in rows]
    valid_times = [ts for ts in parsed_times if ts is not None]
    if not rows or not valid_times:
        return None
    max_horizon = max((parse_horizon(horizon) for horizon in horizons), default=timedelta())
    return {
        "symbols": sorted({str(row.get("symbol")) for row in rows if row.get("symbol")}),
        "start": (min(valid_times) - timedelta(days=1)).date().isoformat(),
        "end": (max(valid_times) + max_horizon + timedelta(days=1)).date().isoformat(),
    }


def load_prices_for_audit(
    rows: list[dict],
    *,
    horizons: list[str],
    interval: str,
    load_prices: PriceHistoryLoader | None = None,
) -> dict[str, list[dict]]:
    request = price_window_for_audit_rows(rows, horizons=horizons)
    if request is None:
        return {}
    loader = load_prices or load_prices_yfinance
    return loader(
        list(request["symbols"]),
        start=str(request["start"]),
        end=str(request["end"]),
        interval=interval,
    )
