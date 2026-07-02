"""CLI compatibility for ``python -m trader.daemon``."""

from trader.runtime.daemon import main

raise SystemExit(main())
