"""Source de verite du capital de depart du portefeuille paper."""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_STARTING_CASH = 100_000.0


def load_starting_cash(config_dir: Path) -> float:
    """Lit starting_cash depuis portfolio.yaml, avec fallback transitoire."""
    portfolio = config_dir / "portfolio.yaml"
    if portfolio.exists():
        try:
            cfg = yaml.safe_load(portfolio.read_text(encoding="utf-8")) or {}
            return float(cfg.get("starting_cash", DEFAULT_STARTING_CASH))
        except Exception:
            pass

    universe = config_dir / "universe.yaml"
    if universe.exists():
        try:
            cfg = yaml.safe_load(universe.read_text(encoding="utf-8")) or {}
            if "starting_cash" in cfg:
                return float(cfg["starting_cash"])
        except Exception:
            pass

    return DEFAULT_STARTING_CASH
