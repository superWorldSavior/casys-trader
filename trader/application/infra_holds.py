"""Compatibility shim for trader.application.cycle.infra_holds."""

import sys as _sys
from trader.application.cycle import infra_holds as _impl
from trader.application.cycle.infra_holds import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
