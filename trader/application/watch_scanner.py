"""Compatibility shim for trader.application.cycle.watch_scanner."""

import sys as _sys
from trader.application.cycle import watch_scanner as _impl
from trader.application.cycle.watch_scanner import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
