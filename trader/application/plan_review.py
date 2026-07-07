"""Compatibility shim for trader.application.record.plan_review."""

import sys as _sys
from trader.application.record import plan_review as _impl
from trader.application.record.plan_review import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
