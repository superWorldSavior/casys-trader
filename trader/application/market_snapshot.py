"""Compatibility shim for trader.application.cycle.market_snapshot."""

import sys as _sys
from trader.application.cycle import market_snapshot as _impl
from trader.application.cycle.market_snapshot import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
