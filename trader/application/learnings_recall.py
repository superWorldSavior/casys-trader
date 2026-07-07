"""Compatibility shim for trader.application.decide.learnings_recall."""

import sys as _sys
from trader.application.decide import learnings_recall as _impl
from trader.application.decide.learnings_recall import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
