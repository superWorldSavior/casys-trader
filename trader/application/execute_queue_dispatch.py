"""Compatibility shim for trader.application.execute.execute_queue_dispatch."""

import sys as _sys
from trader.application.execute import execute_queue_dispatch as _impl
from trader.application.execute.execute_queue_dispatch import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
