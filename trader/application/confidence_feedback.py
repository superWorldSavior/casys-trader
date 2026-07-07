"""Compatibility shim for trader.application.record.confidence_feedback."""

import sys as _sys
from trader.application.record import confidence_feedback as _impl
from trader.application.record.confidence_feedback import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
