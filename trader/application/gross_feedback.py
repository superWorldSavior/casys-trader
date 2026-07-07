"""Compatibility shim for trader.application.record.gross_feedback."""

import sys as _sys
from trader.application.record import gross_feedback as _impl
from trader.application.record.gross_feedback import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
