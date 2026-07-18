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
    python scripts/llm_usage.py --log <path> daily  # autre fichier de log

Provider = le tier acpx effectif : ``acpx`` (trader), ``company-micro``,
``universe``, ``consolidator`` (macro-news), ``acpx-claude-sonnet`` (fallback)…
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_DEFAULT_LOG = Path("state/daemon_console.log")

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


def main(argv: list[str]) -> None:
    args = list(argv)
    log_path = _DEFAULT_LOG
    if "--log" in args:
        idx = args.index("--log")
        try:
            log_path = Path(args[idx + 1])
        except IndexError:
            sys.exit("--log requires a path")
        del args[idx : idx + 2]

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
    else:
        sys.exit(f"unknown command: {command} (overview | daily [provider] | outcomes)")


if __name__ == "__main__":
    main(sys.argv[1:])
