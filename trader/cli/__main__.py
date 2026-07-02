"""CLI compatibility for ``python -m trader.cli``."""

from trader.runtime.cli import main

raise SystemExit(main())
