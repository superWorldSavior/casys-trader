"""Compatibility module for ``trader.reporting.tool_usage``.

The canonical runnable module is ``trader.interfaces.cli.tool_usage``; this shim keeps
old imports and ``python -m trader.tool_usage`` working without a top-level
package.
"""

from trader.reporting.tool_usage import *  # noqa: F403
from trader.interfaces.cli.tool_usage import main as main


if __name__ == "__main__":
    main()
