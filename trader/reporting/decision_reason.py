"""Compatibility facade for decision reason-code vocabulary."""

from trader.domain.decision_reason import (
    REASON_CODES,
    infer_reason_code,
    normalize_reason_code,
    reason_code_enum_text,
)

__all__ = [
    "REASON_CODES",
    "infer_reason_code",
    "normalize_reason_code",
    "reason_code_enum_text",
]
