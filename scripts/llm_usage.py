#!/usr/bin/env python3
"""Conso LLM par provider/jour — SANS migration, depuis les logs du daemon.

Chaque appel LLM émet une ligne structurée ``[acpx_call]`` dans
``state/daemon_console.log`` (cf. ``trader/infrastructure/llm/acpx_backend.py``).
Ce script les agrège par jour et par provider. Il compte les APPELS (proxy de
conso) et leur durée : acpx ne remonte pas le nombre de tokens, donc le volume
d'appels par provider est le meilleur proxy disponible pour mesurer l'effet des
gardes de cadence (posture pré-open, company-micro event-driven, …).

Usage :
    python scripts/llm_usage.py overview            # total par provider
    python scripts/llm_usage.py daily               # par jour x provider (appels)
    python scripts/llm_usage.py daily universe      # idem, filtré sur un provider
    python scripts/llm_usage.py outcomes            # ok / timeout / retryable ... par provider
    python scripts/llm_usage.py cost                # jour×provider + tokens + p50/p95 étages
    python scripts/llm_usage.py --log <path> daily  # autre fichier de log
    python scripts/llm_usage.py cost --price-per-mtok grok=3 --price-per-mtok acpx=5

Provider = le tier acpx effectif : ``acpx`` (trader), ``company-micro``,
``universe``, ``consolidator`` (macro-news), ``acpx-claude-sonnet`` (fallback)…
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_DEFAULT_LOG = Path("state/daemon_console.log")
_DEFAULT_EVENTS = Path("state/events.jsonl")
_DEFAULT_USAGE_DIR = Path("state/archive/llm_usage")
_STAGE_ORDER = (
    "snapshot_ms",
    "gate_scope_ms",
    "decide_ms",
    "risk_execute_ms",
    "record_ms",
    "total_ms",
)

# 2026-07-17 23:21:25,921 INFO [acpx_call] session=... provider=universe timeout_s=120 dur_s=1.7 outcome=ok
_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}).*\[acpx_call\].*?"
    r"provider=(?P<provider>[A-Za-z0-9_-]+).*?"
    r"dur_s=(?P<dur>[0-9.]+).*?outcome=(?P<outcome>[a-z_]+)"  # tolerant to fields inserted between
)


def _rows(log_path: Path):
    """Yield (date, provider, dur_s, outcome) for every acpx_call line."""
    try:
        handle = log_path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.exit(f"cannot read log: {exc}")
    with handle:
        for line in handle:
            match = _LINE.match(line)
            if match:
                yield (
                    match["date"],
                    match["provider"],
                    float(match["dur"]),
                    match["outcome"],
                )


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def overview(rows) -> None:
    calls: Counter[str] = Counter()
    dur: defaultdict[str, float] = defaultdict(float)
    dates: set[str] = set()
    for date, provider, dur_s, _outcome in rows:
        calls[provider] += 1
        dur[provider] += dur_s
        dates.add(date)
    total = sum(calls.values())
    span = f"{min(dates)} → {max(dates)}" if dates else "(aucune donnée)"
    print(f"Fenêtre : {span}  |  jours : {len(dates)}  |  appels totaux : {_fmt_int(total)}\n")
    print(f"{'provider':<22}{'appels':>10}{'part':>8}{'dur_tot_s':>12}{'dur_moy_s':>11}")
    for provider, n in calls.most_common():
        share = 100 * n / total if total else 0
        avg = dur[provider] / n if n else 0
        print(f"{provider:<22}{_fmt_int(n):>10}{share:>7.0f}%{dur[provider]:>12.0f}{avg:>11.1f}")


def daily(rows, provider_filter: str | None) -> None:
    grid: defaultdict[str, Counter[str]] = defaultdict(Counter)
    providers: set[str] = set()
    for date, provider, _dur_s, _outcome in rows:
        if provider_filter and provider != provider_filter:
            continue
        grid[date][provider] += 1
        providers.add(provider)
    if not grid:
        print("(aucun appel correspondant)")
        return
    cols = sorted(providers)
    header = f"{'date':<12}" + "".join(f"{c[:14]:>15}" for c in cols) + f"{'TOTAL':>9}"
    print(header)
    for date in sorted(grid):
        row = grid[date]
        cells = "".join(f"{_fmt_int(row.get(c, 0)):>15}" for c in cols)
        print(f"{date:<12}{cells}{_fmt_int(sum(row.values())):>9}")


def outcomes(rows) -> None:
    grid: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for _date, provider, _dur_s, outcome in rows:
        grid[provider][outcome] += 1
    order = ["ok", "nonzero_retryable", "nonzero", "timeout", "error", "unavailable"]
    seen_extra = sorted({o for c in grid.values() for o in c} - set(order))
    cols = [o for o in order if any(o in c for c in grid.values())] + seen_extra
    header = f"{'provider':<22}" + "".join(f"{o[:16]:>17}" for o in cols)
    print(header)
    for provider in sorted(grid, key=lambda p: -sum(grid[p].values())):
        counts = grid[provider]
        cells = "".join(f"{_fmt_int(counts.get(o, 0)):>17}" for o in cols)
        print(f"{provider:<22}{cells}")


def _take_flag(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    idx = args.index(name)
    try:
        value = args[idx + 1]
    except IndexError:
        sys.exit(f"{name} requires a value")
    del args[idx : idx + 2]
    return value


def _parse_price_per_mtok(args: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    while "--price-per-mtok" in args:
        raw = _take_flag(args, "--price-per-mtok")
        if raw is None or "=" not in raw:
            sys.exit("--price-per-mtok expects provider=price (USD / million tokens)")
        provider, value = raw.split("=", 1)
        try:
            prices[provider.strip()] = float(value)
        except ValueError:
            sys.exit(f"invalid price: {value!r}")
    return prices


def _load_cycle_timings(events_path: Path, *, recent: int) -> dict[str, list[int]]:
    by_stage: dict[str, list[int]] = defaultdict(list)
    if not events_path.is_file():
        return by_stage
    recent_rows: list[dict] = []
    try:
        handle = events_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return by_stage
    with handle:
        for line in handle:
            if "cycle_completed" not in line or "stage_timings_ms" not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "cycle_completed":
                continue
            timings = row.get("stage_timings_ms")
            if isinstance(timings, dict):
                recent_rows.append(timings)
    for timings in recent_rows[-recent:]:
        for stage, value in timings.items():
            try:
                by_stage[str(stage)].append(int(value))
            except (TypeError, ValueError):
                continue
    return by_stage


def _dedupe_token_rows(rows: list[dict]) -> list[dict]:
    """Garde le snapshot le plus élevé par (source, session) pour grain session/thread."""

    best: dict[tuple[str, str], dict] = {}
    passthrough: list[dict] = []
    for row in rows:
        if row.get("proxy"):
            continue
        grain = str(row.get("grain") or "")
        if grain == "thread":
            continue
        source = str(row.get("source") or "")
        session = str(row.get("session") or row.get("id") or "")
        if grain in {"session", "thread"} and session:
            key = (source, session)
            current = best.get(key)
            current_total = int((current or {}).get("total_tokens") or 0)
            new_total = int(row.get("total_tokens") or 0)
            if current is None or new_total >= current_total:
                best[key] = row
            continue
        passthrough.append(row)
    return passthrough + list(best.values())


def _llm_cost():
    try:
        from scripts import llm_cost
    except ImportError:  # python scripts/llm_usage.py (sys.path = scripts/)
        import llm_cost  # type: ignore
    return llm_cost


def cost(
    rows,
    *,
    usage_dir: Path,
    events_path: Path,
    prices: dict[str, float],
    recent_cycles: int,
) -> None:
    calls: dict[tuple[str, str], int] = defaultdict(int)
    dur: dict[tuple[str, str], float] = defaultdict(float)
    for date, provider, dur_s, _outcome in rows:
        key = (date, provider)
        calls[key] += 1
        dur[key] += dur_s

    llm_cost = _llm_cost()
    token_rows = _dedupe_token_rows(llm_cost.load_usage_rows(usage_dir))
    tokens: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cached": 0, "total": 0, "n": 0}
    )
    for row in token_rows:
        date = str(row.get("date") or "")
        provider = str(row.get("provider") or "")
        if not date or not provider:
            continue
        bucket = tokens[(date, provider)]
        bucket["input"] += int(row.get("input_tokens") or 0)
        bucket["output"] += int(row.get("output_tokens") or 0)
        bucket["cached"] += int(row.get("cached_tokens") or 0)
        bucket["total"] += int(row.get("total_tokens") or 0)
        bucket["n"] += int(row.get("n_calls") or 1)

    keys = sorted(set(calls) | set(tokens))
    print(
        f"{'date':<12}{'provider':<18}{'n':>7}{'dur_s':>10}"
        f"{'tok_in':>10}{'tok_out':>10}{'tok_cached':>12}{'tok_tot':>10}{'usd':>10}"
    )
    if not keys:
        print("(aucune donnée)")
    for date, provider in keys:
        n = calls.get((date, provider), 0)
        duration = dur.get((date, provider), 0.0)
        tok = tokens.get((date, provider))
        if tok:
            tok_cells = (
                f"{_fmt_int(tok['input']):>10}{_fmt_int(tok['output']):>10}"
                f"{_fmt_int(tok['cached']):>12}{_fmt_int(tok['total']):>10}"
            )
            usd = ""
            price = prices.get(provider)
            if price is not None and tok["total"]:
                usd = f"{tok['total'] / 1_000_000.0 * price:,.2f}"
            usd_cell = f"{usd:>10}"
        else:
            tok_cells = f"{'-':>10}{'-':>10}{'-':>12}{'-':>10}"
            usd_cell = f"{'-':>10}"
        print(
            f"{date:<12}{provider:<18}{_fmt_int(n):>7}{duration:>10.1f}{tok_cells}{usd_cell}"
        )

    timings = _load_cycle_timings(events_path, recent=recent_cycles)
    print()
    print(f"stage_timings_ms (p50/p95, {recent_cycles} cycles récents avec timings)")
    print(f"{'stage':<18}{'n':>7}{'p50':>10}{'p95':>10}")
    stages = [stage for stage in _STAGE_ORDER if stage in timings] + [
        stage for stage in sorted(timings) if stage not in _STAGE_ORDER
    ]
    if not stages:
        print("(aucun stage_timings_ms dans events.jsonl)")
        return
    for stage in stages:
        values = timings[stage]
        p50 = llm_cost.percentile(values, 50)
        p95 = llm_cost.percentile(values, 95)
        print(f"{stage:<18}{_fmt_int(len(values)):>7}{p50:>10}{p95:>10}")


def main(argv: list[str]) -> None:
    args = list(argv)
    log_path = _DEFAULT_LOG
    events_path = _DEFAULT_EVENTS
    usage_dir = _DEFAULT_USAGE_DIR
    recent_cycles = 200
    raw_log = _take_flag(args, "--log")
    if raw_log:
        log_path = Path(raw_log)
    raw_events = _take_flag(args, "--events")
    if raw_events:
        events_path = Path(raw_events)
    raw_usage = _take_flag(args, "--usage-dir")
    if raw_usage:
        usage_dir = Path(raw_usage)
    raw_recent = _take_flag(args, "--recent-cycles")
    if raw_recent:
        try:
            recent_cycles = max(1, int(raw_recent))
        except ValueError:
            sys.exit("--recent-cycles expects an integer")
    prices = _parse_price_per_mtok(args)

    command = args[0] if args else "overview"
    if command in ("-h", "--help", "help"):
        print(__doc__)
        return
    rows = list(_rows(log_path))

    if command == "overview":
        overview(rows)
    elif command == "daily":
        daily(rows, args[1] if len(args) > 1 else None)
    elif command == "outcomes":
        outcomes(rows)
    elif command == "cost":
        cost(
            rows,
            usage_dir=usage_dir,
            events_path=events_path,
            prices=prices,
            recent_cycles=recent_cycles,
        )
    else:
        sys.exit(f"unknown command: {command} (overview | daily [provider] | outcomes | cost)")


if __name__ == "__main__":
    main(sys.argv[1:])
