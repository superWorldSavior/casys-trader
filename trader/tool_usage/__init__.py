"""Compatibility package for ``trader.reporting.tool_usage``.

The canonical runnable module is ``trader.commands.tool_usage``; this package
keeps old imports such as ``trader.tool_usage`` working.
"""

from trader.reporting.tool_usage import *  # noqa: F403
