"""Canonical CLI entrypoint for tool-usage reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtest.decision_quality import BAND

from trader.reporting.tool_usage import build_report, render_cli

STATE_DIR = Path("state")
DEFAULT_LEDGER = STATE_DIR / "decisions.jsonl"
OUTPUT_PATH = STATE_DIR / "last_tool_usage.json"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Usage des canaux d'outils vs qualité forward")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="ledger JSONL des décisions")
    parser.add_argument("--band", type=float, default=BAND, help="bande significative de rendement forward")
    parser.add_argument("--days-buffer", type=int, default=1, help="jours de marge avant la première décision")
    parser.add_argument("--json", action="store_true", help="affiche le rapport complet en JSON")
    args = parser.parse_args(argv)

    report = build_report(
        args.ledger,
        band=args.band,
        days_buffer=args.days_buffer,
        output_path=OUTPUT_PATH,
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_cli(report))


if __name__ == "__main__":
    main()
