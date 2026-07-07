"""Compatibility shim for trader.application.cycle.execution_eligibility."""

import sys as _sys
from trader.application.cycle import execution_eligibility as _impl
from trader.application.cycle.execution_eligibility import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
