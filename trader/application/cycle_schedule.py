"""Compatibility shim for trader.application.cycle.cycle_schedule."""

import sys as _sys
from trader.application.cycle import cycle_schedule as _impl
from trader.application.cycle.cycle_schedule import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
