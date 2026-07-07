"""Compatibility shim for trader.application.decide.tool_round."""

import sys as _sys
from trader.application.decide import tool_round as _impl
from trader.application.decide.tool_round import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
