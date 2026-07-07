"""Compatibility shim for trader.application.migration.strategy_language_migration."""

import sys as _sys
from trader.application.migration import strategy_language_migration as _impl
from trader.application.migration.strategy_language_migration import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
