"""Compatibility shim for trader.application.decide.recent_decisions."""

import sys as _sys
from trader.application.decide import recent_decisions as _impl
from trader.application.decide.recent_decisions import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
