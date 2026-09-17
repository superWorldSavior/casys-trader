"""Yahoo exchange → MIC cross-check table. Stdlib/domain only.

The WorldScopeMapping is the listing authority: instrument MICs always come
from mapping entries, never from this table. This table only cross-checks the
``exchange`` code carried by company briefs against the mapped MIC; a conflict
excludes the symbol from issuer derivation (fail-closed, counted). Unknown or
missing exchange codes yield ``None`` (mapping-only resolution): the mapping
stays authoritative, the brief code is auxiliary evidence.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

EXCHANGE_MIC_TABLE_VERSION = "yahoo_exchange_mic.v1"

YAHOO_EXCHANGE_MICS = MappingProxyType(
    {
        "AMS": "XAMS",
        "BRU": "XBRU",
        "CPH": "XCSE",
        "EBS": "XSWX",
        "GER": "XETR",
        "HEL": "XHEL",
        "LIS": "XLIS",
        "LSE": "XLON",
        "MCE": "XMAD",
        "MIL": "XMIL",
        "NMS": "XNAS",
        "NYQ": "XNYS",
        "OSL": "XOSL",
        "PAR": "XPAR",
        "STO": "XSTO",
        "TAI": "XTAI",
        # TPEx (`TWO`) folds into XTAI by mapping convention; the mapping wins.
        "TWO": "XTAI",
        "VIE": "XWBO",
    }
)


def mic_for_yahoo_exchange(exchange: Any) -> str | None:
    """Mapped MIC for a Yahoo exchange code, or ``None`` when unknown/absent."""

    if exchange is None:
        return None
    if not isinstance(exchange, str):
        raise TypeError("exchange must be a string or None")
    code = exchange.strip().upper()
    if not code:
        return None
    return YAHOO_EXCHANGE_MICS.get(code)


__all__ = [
    "EXCHANGE_MIC_TABLE_VERSION",
    "YAHOO_EXCHANGE_MICS",
    "mic_for_yahoo_exchange",
]
