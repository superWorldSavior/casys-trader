"""Compatibility shim for trader.application.execute.entry_context."""

import sys as _sys
from trader.application.execute import entry_context as _impl
from trader.application.execute.entry_context import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
