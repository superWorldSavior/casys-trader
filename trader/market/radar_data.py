"""Compatibility facade for radar market-data I/O."""

from __future__ import annotations

import sys as _sys

from trader.infrastructure.market_sources import radar_data as _impl

_sys.modules[__name__] = _impl
