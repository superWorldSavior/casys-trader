"""Compatibility facade for macro-series I/O collection."""

from __future__ import annotations

import sys as _sys

from trader.infrastructure.market_sources import macro_series as _impl

_sys.modules[__name__] = _impl
