"""Compatibility shim for trader.application.record.decision_entries."""

import sys as _sys
from trader.application.record import decision_entries as _impl
from trader.application.record.decision_entries import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
