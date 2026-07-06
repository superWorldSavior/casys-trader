"""Canonical CLI entrypoint for attribution reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trader.reporting.attribution import compute_attribution, render_text

DEFAULT_STATE_DIR = Path(__file__).resolve().parents[3] / "state"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Attribution décision->résultat du trader paper")
    parser.add_argument("--json", action="store_true", help="sortie JSON compact")
    args = parser.parse_args(argv)

    attr = compute_attribution(DEFAULT_STATE_DIR)

    if args.json:
        print(json.dumps(attr, separators=(",", ":"), ensure_ascii=False))
    else:
        print(render_text(attr))


if __name__ == "__main__":
    main()
