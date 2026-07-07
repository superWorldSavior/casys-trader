"""Compatibility shim for trader.application.exit.exit_update."""

import sys as _sys
from trader.application.exit import exit_update as _impl
from trader.application.exit.exit_update import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
