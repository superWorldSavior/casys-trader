"""Compatibility shim for trader.application.exit.fill_plan_effects."""

import sys as _sys
from trader.application.exit import fill_plan_effects as _impl
from trader.application.exit.fill_plan_effects import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
