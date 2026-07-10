"""Rich meters used by cockpit pages."""

from __future__ import annotations

from rich.text import Text

from trader.interfaces.ui.palette import CASYS_ACCENT, CASYS_METER_EMPTY
from trader.support.coercion import finite_float


def confidence_meter(confidence: object, *, width: int = 10) -> Text:
    """Render a confidence score as a fixed-width Rich meter."""

    number = finite_float(confidence, default=None)
    text = Text()
    if number is None:
        text.append("▮" * width, style=CASYS_METER_EMPTY)
        text.append("  —")
        return text
    filled = max(0, min(width, round(number * width)))
    text.append("▮" * filled, style=CASYS_ACCENT)
    text.append("▮" * (width - filled), style=CASYS_METER_EMPTY)
    text.append(f"  {number:.2f}")
    return text


__all__ = ["confidence_meter"]
