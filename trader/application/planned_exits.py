"""Compatibility shim for trader.application.exit.planned_exits."""

import sys as _sys
from trader.application.exit import planned_exits as _impl
from trader.application.exit.planned_exits import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
