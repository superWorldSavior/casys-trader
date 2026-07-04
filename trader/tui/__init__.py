"""Compatibility package for ``trader.ui.tui``.

Historically tests and scripts imported private Rich/read-model helpers from
``trader.tui``. Re-export every non-dunder symbol so that moving the module into
``trader.ui`` does not break those imports. The canonical runnable module is
``trader.commands.tui``.
"""

from trader.ui import tui as _tui

_EXPORT_NAMES = [_name for _name in dir(_tui) if not _name.startswith("__")]

for _name in _EXPORT_NAMES:
    globals()[_name] = getattr(_tui, _name)

__all__ = list(_EXPORT_NAMES)

del _EXPORT_NAMES, _name, _tui
