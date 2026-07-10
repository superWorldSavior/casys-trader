from __future__ import annotations

import math

from trader.support.coercion import dict_list, finite_float


def test_finite_float_accepts_numeric_values_and_rejects_non_finite_values() -> None:
    assert finite_float("1.25") == 1.25
    assert finite_float(4) == 4.0
    assert finite_float(float("nan"), default=None) is None
    assert finite_float(float("inf"), default=-1.0) == -1.0
    assert math.isfinite(finite_float("2") or 0.0)


def test_finite_float_returns_the_requested_default_for_invalid_values() -> None:
    assert finite_float(None) == 0.0
    assert finite_float("not-a-number", default=None) is None


def test_dict_list_filters_non_dict_rows_and_rejects_other_containers() -> None:
    first = {"symbol": "SPY"}
    second = {"symbol": "QQQ"}

    assert dict_list([first, "noise", second, None]) == [first, second]
    assert dict_list((first, second)) == []
    assert dict_list(None) == []
