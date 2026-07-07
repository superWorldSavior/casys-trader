"""Compatibility shim for trader.application.record.decision_watches."""

import sys as _sys
from trader.application.record import decision_watches as _impl
from trader.application.record.decision_watches import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
