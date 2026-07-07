"""Compatibility shim for trader.application.decide.decide_one."""

import sys as _sys
from trader.application.decide import decide_one as _impl
from trader.application.decide.decide_one import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
