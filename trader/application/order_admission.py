"""Compatibility shim for trader.application.execute.order_admission."""

import sys as _sys
from trader.application.execute import order_admission as _impl
from trader.application.execute.order_admission import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
