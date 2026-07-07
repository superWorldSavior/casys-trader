"""Compatibility shim for trader.application.exit.armed_plans."""

import sys as _sys
from trader.application.exit import armed_plans as _impl
from trader.application.exit.armed_plans import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
