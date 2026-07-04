"""Compatibility package for ``trader.reporting.stats``.

The canonical runnable module is ``trader.commands.stats``; this package keeps
old imports such as ``trader.stats`` working.
"""

from trader.reporting.stats import *  # noqa: F403
