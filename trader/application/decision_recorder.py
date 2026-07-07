"""Compatibility shim for trader.application.record.decision_recorder."""

import sys as _sys
from trader.application.record import decision_recorder as _impl
from trader.application.record.decision_recorder import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
