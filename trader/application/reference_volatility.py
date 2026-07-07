"""Compatibility shim for trader.application.exit.reference_volatility."""

import sys as _sys
from trader.application.exit import reference_volatility as _impl
from trader.application.exit.reference_volatility import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
