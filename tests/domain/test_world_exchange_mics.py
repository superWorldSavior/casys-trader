from __future__ import annotations

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_exchange_mics import (
    EXCHANGE_MIC_TABLE_VERSION,
    YAHOO_EXCHANGE_MICS,
    mic_for_yahoo_exchange,
)

MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_exchange_mics.py"


def test_table_covers_observed_brief_exchanges() -> None:
    assert EXCHANGE_MIC_TABLE_VERSION == "yahoo_exchange_mic.v1"
    assert mic_for_yahoo_exchange("PAR") == "XPAR"
    assert mic_for_yahoo_exchange("TAI") == "XTAI"
    assert mic_for_yahoo_exchange("TWO") == "XTAI"
    assert mic_for_yahoo_exchange("NMS") == "XNAS"
    assert mic_for_yahoo_exchange("NYQ") == "XNYS"
    assert mic_for_yahoo_exchange("EBS") == "XSWX"
    assert mic_for_yahoo_exchange("MCE") == "XMAD"
    assert all(len(mic) == 4 for mic in YAHOO_EXCHANGE_MICS.values())


def test_unknown_and_missing_exchanges_degrade_to_none() -> None:
    assert mic_for_yahoo_exchange(None) is None
    assert mic_for_yahoo_exchange("") is None
    assert mic_for_yahoo_exchange("   ") is None
    assert mic_for_yahoo_exchange("XXX") is None
    assert mic_for_yahoo_exchange("par") == "XPAR"
    with pytest.raises(TypeError, match="string or None"):
        mic_for_yahoo_exchange(42)  # type: ignore[arg-type]


def test_world_exchange_mics_stays_stdlib_domain() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
