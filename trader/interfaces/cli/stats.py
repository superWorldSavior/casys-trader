"""Canonical CLI entrypoint for live KPI reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trader.reporting.read_models.live_kpis import compute_live_kpis
from trader.reporting.renderers.live_kpis import render_text

DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / "state"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="KPI live du trader paper")
    parser.add_argument("--json", action="store_true", help="sortie JSON compact")
    args = parser.parse_args(argv)

    kpis = compute_live_kpis(DEFAULT_STATE_DIR)

    if args.json:
        print(json.dumps(kpis, separators=(",", ":"), ensure_ascii=False))
    else:
        print(render_text(kpis))


if __name__ == "__main__":
    main()
