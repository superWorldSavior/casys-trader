"""Compatibility shim for trader.application.decide.planner_batch."""

import sys as _sys
from trader.application.decide import planner_batch as _impl
from trader.application.decide.planner_batch import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
