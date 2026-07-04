"""Compatibility module for historical ``trader.tui`` imports."""

from trader.interfaces.cli.tui import main as _command_main
from trader.interfaces.ui import tui as _tui

_EXPORT_NAMES = [_name for _name in dir(_tui) if not _name.startswith("__")]

for _name in _EXPORT_NAMES:
    globals()[_name] = getattr(_tui, _name)

__all__ = list(_EXPORT_NAMES)

del _EXPORT_NAMES, _name, _tui


if __name__ == "__main__":
    _command_main()
