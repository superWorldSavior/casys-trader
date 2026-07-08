"""Compatibility facade for FX-rate market-source I/O."""

from __future__ import annotations

import sys as _sys

from trader.infrastructure.market_sources import fx_rates as _impl

_sys.modules[__name__] = _impl
