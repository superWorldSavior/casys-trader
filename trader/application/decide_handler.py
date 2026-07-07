"""Compatibility shim for trader.application.decide.decide_handler."""

import sys as _sys
from trader.application.decide import decide_handler as _impl
from trader.application.decide.decide_handler import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
