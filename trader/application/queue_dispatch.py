"""Compatibility shim for trader.application.decide.queue_dispatch."""

import sys as _sys
from trader.application.decide import queue_dispatch as _impl
from trader.application.decide.queue_dispatch import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
