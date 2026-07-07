"""Compatibility shim for trader.application.record.tool_outcomes."""

import sys as _sys
from trader.application.record import tool_outcomes as _impl
from trader.application.record.tool_outcomes import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
