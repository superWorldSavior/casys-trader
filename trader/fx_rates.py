# trader/fx_rates.py
"""fx_rates — provider de taux FX vers USD (live yfinance + fallback statique).

I/O isolée ici : le fetch est injecté (testable). Le module fx reste pur.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import yaml

from . import fx

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], float | None]


def load_fx_config(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {str(k): dict(v) for k, v in data.items()}


def _rate_from_close(close: float | None, *, invert: bool) -> float | None:
    if close is None or close <= 0.0:
        return None
    return (1.0 / close) if invert else close


def rates_for_symbols(symbols, *, fetcher: Fetcher, config: dict) -> dict[str, float]:
    currencies = {fx.currency_for(s) for s in symbols}
    rates: dict[str, float] = {fx.BASE_CCY: 1.0}
    for ccy in currencies:
        if ccy == fx.BASE_CCY:
            continue
        spec = config.get(ccy)
        if spec is None:
            raise ValueError(f"devise non configurée dans fx.yaml: {ccy}")
        rate = None
        try:
            rate = _rate_from_close(fetcher(spec["yahoo"]), invert=bool(spec.get("invert")))
        except Exception as exc:  # noqa: BLE001 — fail-safe : on dégrade au fallback.
            logger.warning("fx fetch %s échoué (%s), fallback statique", ccy, exc)
        if rate is None:
            rate = float(spec["fallback"])
            logger.warning("fx %s : taux fallback statique %s", ccy, rate)
        rates[ccy] = rate
    return rates
