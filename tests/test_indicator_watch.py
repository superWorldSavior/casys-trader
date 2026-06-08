from datetime import datetime, timezone

from trader.indicator_watch import (
    WATCH_REJECT_INVALID_OPERATOR,
    WATCH_REJECT_MISSING_THRESHOLD,
    WATCH_REJECT_NON_FINITE_THRESHOLD,
    WATCH_REJECT_UNKNOWN_INDICATOR,
    build_indicator_watch,
    evaluate_indicator_watches,
    normalize_indicator_watch,
)
from trader.tools.market import Bar


def _bar(ts: str, close: float) -> Bar:
    return Bar(
        ts=ts,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1000.0,
    )


def _bars(*closes: float) -> list[Bar]:
    return [_bar(f"t{index}", close) for index, close in enumerate(closes)]


def test_normalize_indicator_watch_borne_et_persiste_une_combinaison_multi_timeframe() -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    watch = normalize_indicator_watch(
        {
            "ttl_minutes": 90,
            "logic": "all",
            "on_trigger": "ORDER",
            "conditions": [
                {"indicator": "z_score", "op": ">=", "value": 1.8, "interval": "15m", "window": 32},
                {"symbol": "QQQ", "indicator": "return", "op": ">", "value": 0.01, "interval": "4h", "lookback": "1mo", "window": 24},
            ],
            "order": {"action": "BUY", "quantity": 10, "intent": "OPEN_LONG"},
        },
        owner_symbol="SPY",
        now=now,
    )

    assert watch is not None
    assert watch["symbol"] == "SPY"
    assert watch["logic"] == "all"
    assert watch["on_trigger"] == "WAKE_WITH_ORDER_INTENT"
    assert watch["expires_at"] == "2026-06-05T13:30:00+00:00"
    assert watch["conditions"][0]["symbol"] == "SPY"
    assert watch["conditions"][0]["interval"] == "15m"
    assert watch["conditions"][1]["symbol"] == "QQQ"
    assert watch["conditions"][1]["interval"] == "4h"
    assert watch["conditions"][1]["source_interval"] == "1h"
    assert watch["conditions"][1]["lookback"] == "1mo"
    assert watch["order"]["action"] == "BUY"


def test_normalize_indicator_watch_ignore_les_conditions_sans_seuil_numerique() -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    empty = normalize_indicator_watch(
        {"conditions": [{"indicator": "z_score", "op": ">=", "value": None}]},
        owner_symbol="SPY",
        now=now,
    )
    mixed = normalize_indicator_watch(
        {
            "conditions": [
                {"indicator": "z_score", "op": ">=", "value": None},
                {"indicator": "return", "op": ">", "value": "not-a-number"},
                {"indicator": "efficiency_ratio", "op": ">", "value": "NaN"},
                {"indicator": "trend_slope", "op": "<", "threshold": 0.0},
                {"indicator": "range_position", "op": ">", "value": None, "threshold": 0.5},
            ]
        },
        owner_symbol="SPY",
        now=now,
    )

    assert empty is None
    assert mixed is not None
    assert mixed["conditions"] == [
        {
            "symbol": "SPY",
            "indicator": "trend_slope",
            "op": "<",
            "value": 0.0,
            "interval": "1h",
            "timeframe": "1h",
            "source_interval": "1h",
            "lookback": "5d",
            "window": 48,
            "as_of": "latest",
        },
        {
            "symbol": "SPY",
            "indicator": "range_position",
            "op": ">",
            "value": 0.5,
            "interval": "1h",
            "timeframe": "1h",
            "source_interval": "1h",
            "lookback": "5d",
            "window": 48,
            "as_of": "latest",
        }
    ]


def test_build_indicator_watch_remonte_les_rejets_par_condition() -> None:
    """Les rejets de conditions sont exposés dans l'ordre avec raison et valeur fautive."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    result = build_indicator_watch(
        {
            "conditions": [
                {"indicator": "z_score", "op": ">=", "value": None},
                {"indicator": "inconnu", "op": ">=", "value": 1.0},
                {"indicator": "return", "op": "??", "value": 0.01},
                {"indicator": "trend_slope", "op": "<"},
                {"indicator": "range_position", "op": ">", "value": 0.5},
            ]
        },
        owner_symbol="SPY",
        now=now,
    )

    assert result.watch is not None
    assert [condition["indicator"] for condition in result.watch["conditions"]] == ["range_position"]
    assert result.rejections == [
        {"reason": WATCH_REJECT_NON_FINITE_THRESHOLD, "indicator": "z_score", "raw_value": None},
        {"reason": WATCH_REJECT_UNKNOWN_INDICATOR, "indicator": "inconnu", "raw_value": None},
        {"reason": WATCH_REJECT_INVALID_OPERATOR, "indicator": "return", "raw_value": "??"},
        {"reason": WATCH_REJECT_MISSING_THRESHOLD, "indicator": "trend_slope", "raw_value": None},
    ]


def test_build_indicator_watch_rejet_total_donne_watch_none_avec_raisons() -> None:
    """Un rejet total ne crée pas de watch mais conserve la raison exploitable."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    result = build_indicator_watch(
        {"conditions": [{"indicator": "z_score", "op": ">=", "value": "NaN"}]},
        owner_symbol="SPY",
        now=now,
    )

    assert result.watch is None
    assert result.rejections == [
        {"reason": WATCH_REJECT_NON_FINITE_THRESHOLD, "indicator": "z_score", "raw_value": "NaN"}
    ]


def test_evaluate_indicator_watches_declenche_quand_combinaison_est_vraie() -> None:
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    watch = normalize_indicator_watch(
        {
            "ttl_minutes": 90,
            "logic": "all",
            "conditions": [
                {"indicator": "z_score", "op": ">=", "value": 1.0, "interval": "15m", "window": 5},
                {"indicator": "return", "op": ">", "value": 0.08, "interval": "1h", "window": 5},
            ],
        },
        owner_symbol="SPY",
        now=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
    )
    assert watch is not None
    bars = [_bar("t1", 100.0), _bar("t2", 101.0), _bar("t3", 102.0), _bar("t4", 103.0), _bar("t5", 110.0)]

    triggered = evaluate_indicator_watches(
        [watch],
        {
            ("SPY", "15m"): bars,
            ("SPY", "1h"): bars,
        },
        now=now,
    )

    assert len(triggered) == 1
    assert triggered[0]["symbol"] == "SPY"
    assert triggered[0]["on_trigger"] == "WAKE"
    assert triggered[0]["matched"][0]["indicator"] == "z_score"


def test_evaluate_indicator_watches_declenche_relative_strength_avec_pairs() -> None:
    watch = normalize_indicator_watch(
        {
            "ttl_minutes": 90,
            "conditions": [
                {"indicator": "relative_strength", "op": ">", "value": 0.04, "interval": "1h", "window": 5},
            ],
        },
        owner_symbol="SPY",
        now=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
    )
    assert watch is not None

    triggered = evaluate_indicator_watches(
        [watch],
        {
            ("SPY", "1h"): _bars(100.0, 100.0, 100.0, 100.0, 110.0),
            ("QQQ", "1h"): _bars(200.0, 200.0, 200.0, 200.0, 200.0),
            ("DIA", "1h"): _bars(300.0, 300.0, 300.0, 300.0, 300.0),
        },
        now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
    )

    assert len(triggered) == 1
    assert triggered[0]["matched"][0]["indicator"] == "relative_strength"
    assert triggered[0]["matched"][0]["actual"] > 0.04


def test_evaluate_indicator_watches_ignore_les_watches_expirees() -> None:
    watch = normalize_indicator_watch(
        {
            "ttl_minutes": 5,
            "conditions": [{"indicator": "return", "op": ">", "value": 0.01, "interval": "1h", "window": 3}],
        },
        owner_symbol="SPY",
        now=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
    )
    assert watch is not None

    triggered = evaluate_indicator_watches(
        [watch],
        {("SPY", "1h"): [_bar("t1", 100.0), _bar("t2", 105.0), _bar("t3", 110.0)]},
        now=datetime(2026, 6, 5, 12, 6, tzinfo=timezone.utc),
    )

    assert triggered == []
