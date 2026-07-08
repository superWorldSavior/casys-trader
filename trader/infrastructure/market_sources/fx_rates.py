# trader/fx_rates.py
"""fx_rates — provider de taux FX vers USD (live yfinance + fallback statique).

I/O isolée ici : le fetch est injecté (testable). Le module fx reste pur.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Callable

import yaml

from trader.market import fx

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], float | None]


_REQUIRED_KEYS = ("yahoo", "fallback")


def load_fx_config(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    result: dict = {}
    for k, v in data.items():
        ccy = str(k)
        block = dict(v)
        for key in _REQUIRED_KEYS:
            if key not in block:
                raise ValueError(f"fx.yaml: bloc '{ccy}' manque la clé obligatoire '{key}'")
        result[ccy] = block
    return result


def _rate_from_close(close: float | None, *, invert: bool) -> float | None:
    if close is None or not math.isfinite(close) or close <= 0.0:
        return None
    return (1.0 / close) if invert else close


def rates_for_symbols(symbols, *, fetcher: Fetcher, config: dict) -> dict[str, float]:
    """Résout les taux FX pour l'ensemble des devises présentes dans `symbols`.

    Résilience par devise : une devise inconnue ou en erreur ne bloque pas les
    autres. Priorité : fetch live → fallback statique → 1.0 (avec WARNING loud
    si la devise est absente de fx.yaml).
    """
    currencies = {fx.currency_for(s) for s in symbols}
    rates: dict[str, float] = {fx.BASE_CCY: 1.0}
    for ccy in currencies:
        if ccy == fx.BASE_CCY:
            continue
        spec = config.get(ccy)
        if spec is None:
            logger.warning(
                "fx %s : devise non configurée dans fx.yaml — taux forcé à 1.0 (CONVERSION INCORRECTE)",
                ccy,
            )
            rates[ccy] = 1.0
            continue
        rate = None
        exc_info: Exception | None = None
        try:
            rate = _rate_from_close(fetcher(spec["yahoo"]), invert=bool(spec.get("invert")))
        except Exception as exc:  # noqa: BLE001 — fail-safe : on dégrade au fallback.
            exc_info = exc
        if rate is None:
            rate = float(spec["fallback"])
            if exc_info is not None:
                logger.warning("fx %s : fetch échoué (%s), fallback statique %s", ccy, exc_info, rate)
            else:
                logger.warning("fx %s : fetch retourné None, fallback statique %s", ccy, rate)
        rates[ccy] = rate
    return rates
