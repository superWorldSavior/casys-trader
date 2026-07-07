"""Compatibility shim for trader.application.execute.execute_order_handler."""

import sys as _sys
from trader.application.execute import execute_order_handler as _impl
from trader.application.execute.execute_order_handler import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
