from trader.application.entry_context import build_trade_entry_context


def test_build_trade_entry_context_rounds_data_age() -> None:
    assert build_trade_entry_context(
        price=100.5,
        runtime_interval="15m",
        data_age_minutes=3.7,
        session_open=True,
        daily_as_of="2026-07-05",
    ) == {
        "price": 100.5,
        "runtime_interval": "15m",
        "data_age_m": 4,
        "session_open": True,
        "daily_as_of": "2026-07-05",
    }


def test_build_trade_entry_context_preserves_unknown_data_age() -> None:
    assert build_trade_entry_context(
        price=99.0,
        runtime_interval="5m",
        data_age_minutes=None,
        session_open=False,
        daily_as_of=None,
    ) == {
        "price": 99.0,
        "runtime_interval": "5m",
        "data_age_m": None,
        "session_open": False,
        "daily_as_of": None,
    }
