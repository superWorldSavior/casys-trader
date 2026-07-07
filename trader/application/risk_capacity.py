"""Compatibility shim for trader.application.execute.risk_capacity."""

import sys as _sys
from trader.application.execute import risk_capacity as _impl
from trader.application.execute.risk_capacity import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
