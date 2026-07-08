"""Compatibility facade for news-feed I/O."""

from __future__ import annotations

import sys as _sys

from trader.infrastructure.market_sources import news_feed as _impl

__all__ = sorted(name for name in dir(_impl) if not name.startswith("__"))

for _name in __all__:
    globals()[_name] = getattr(_impl, _name)

_impl.__all__ = __all__
_sys.modules[__name__] = _impl
