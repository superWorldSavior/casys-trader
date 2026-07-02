"""Compatibility package for the Textual cockpit application.

The historical ``trader.cockpit`` module is now ``trader.cockpit.app``. Re-export
the app module's non-dunder symbols so existing imports keep working.
"""

from trader.cockpit import app as _app

_EXPORT_NAMES = [_name for _name in dir(_app) if not _name.startswith("__")]

for _name in _EXPORT_NAMES:
    globals()[_name] = getattr(_app, _name)

__all__ = list(_EXPORT_NAMES)

del _EXPORT_NAMES, _name, _app
