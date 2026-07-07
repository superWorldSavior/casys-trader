"""Compatibility shim for trader.application.execute.fill_outcome."""

import sys as _sys
from trader.application.execute import fill_outcome as _impl
from trader.application.execute.fill_outcome import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
