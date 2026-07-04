"""Compatibility module for ``trader.reporting.stats``.

The canonical runnable module is ``trader.commands.stats``; this shim keeps old
imports and ``python -m trader.stats`` working without a top-level package.
"""

from trader.reporting.stats import *  # noqa: F403
from trader.commands.stats import main as main


if __name__ == "__main__":
    main()
