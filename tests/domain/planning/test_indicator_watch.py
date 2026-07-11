from datetime import datetime, timezone

from trader.domain.planning import indicator_watch as indicator_watch_mod
from trader.domain.planning.indicator_watch import (
    WATCH_REJECT_INVALID_OPERATOR,
    WATCH_REJECT_MISSING_THRESHOLD,
    WATCH_REJECT_NON_FINITE_THRESHOLD,
    WATCH_REJECT_UNKNOWN_INDICATOR,
    WATCH_REJECT_UNKNOWN_LABEL,
    build_indicator_watch,
    evaluate_indicator_watches,
    normalize_indicator_watch,
)
from trader.domain.market_data import Bar


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


def test_summarize_watch_resume_un_plan_arme() -> None:
    summary_fn = getattr(indicator_watch_mod, "summarize_watch", None)
    assert summary_fn is not None
    watch = {
        "id": "armed-1",
        "symbol": "SPY",
        "on_trigger": "EXECUTE_ORDER",
        "expires_at": "2026-06-05T14:00:00+00:00",
        "logic": "all",
        "conditions": [
            {
                "indicator": "z_score",
                "op": ">=",
                "value": 1.8,
                "timeframe": "15m",
                "window": 32,
            }
        ],
        "order": {"intent": "OPEN_LONG", "qty": 10},
    }

    assert summary_fn(watch) == {
        "id": "armed-1",
        "kind": "armed",
        "intent": "OPEN_LONG",
        "expires_at": "2026-06-05T14:00:00+00:00",
        "logic": "all",
        "conditions": [{"indicator": "z_score", "op": ">=", "value": 1.8, "timeframe": "15m"}],
    }


def test_summarize_watch_resume_une_veille() -> None:
    watch = {
        "id": "wake-1",
        "symbol": "QQQ",
        "on_trigger": "WAKE",
        "expires_at": "2026-06-05T13:00:00+00:00",
        "conditions": [
            {
                "indicator": "return",
                "op": "<",
                "value": -0.02,
                "timeframe": "1h",
                "source_interval": "1h",
            }
        ],
    }

    assert indicator_watch_mod.summarize_watch(watch) == {
        "id": "wake-1",
        "kind": "wake",
        "expires_at": "2026-06-05T13:00:00+00:00",
        "conditions": [{"indicator": "return", "op": "<", "value": -0.02, "timeframe": "1h"}],
    }


def test_summarize_watch_inclut_logic_quand_present() -> None:
    watch = {
        "id": "wake-logic",
        "symbol": "SPY",
        "on_trigger": "WAKE",
        "logic": "any",
        "conditions": [
            {"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"},
            {"indicator": "z_score", "op": "<", "value": -1.2, "timeframe": "15m"},
        ],
    }

    assert indicator_watch_mod.summarize_watch(watch)["logic"] == "any"


def test_summarize_watch_est_defensif_sans_order_ni_conditions() -> None:
    watch = {
        "id": "partial-1",
        "symbol": "SPY",
        "on_trigger": "EXECUTE_ORDER",
    }

    assert indicator_watch_mod.summarize_watch(watch) == {
        "id": "partial-1",
        "kind": "wake",
        "conditions": [],
    }


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


def test_normalize_indicator_watch_conditions_sans_seuil_invalident_la_watch() -> None:
    # CONTRAT RÉVISÉ (filet atomique, phase 3) : une seule condition rejetée invalide
    # TOUTE la veille — on ne persiste jamais une watch amputée. L'ancien contrat
    # (persistance partielle des conditions valides) a été supprimé pour éviter qu'un
    # logic=all privé d'un prédicat se déclenche sur la condition résiduelle permissive.
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    empty = normalize_indicator_watch(
        {"conditions": [{"indicator": "z_score", "op": ">=", "value": None}]},
        owner_symbol="SPY",
        now=now,
    )
    # 3 conditions rejetées (seuil non-fini) + 2 conditions valides → filet → None
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
    # Le filet atomique invalide la veille dès qu'un rejet est présent
    assert mixed is None

    # Seules toutes les conditions valides → watch persistée
    all_valid = normalize_indicator_watch(
        {
            "conditions": [
                {"indicator": "trend_slope", "op": "<", "threshold": 0.0},
                {"indicator": "range_position", "op": ">", "value": 0.5},
            ]
        },
        owner_symbol="SPY",
        now=now,
    )
    assert all_valid is not None
    assert [c["indicator"] for c in all_valid["conditions"]] == ["trend_slope", "range_position"]


def test_build_indicator_watch_remonte_les_rejets_par_condition() -> None:
    """Les rejets de conditions sont exposés dans l'ordre avec raison et valeur fautive.

    CONTRAT RÉVISÉ (filet atomique, phase 3) : avec 4 rejets + 1 condition valide,
    watch est None (pas de persistance partielle). Les rejets sont toujours exposés.
    """
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

    # Filet atomique : une condition rejetée invalide toute la veille
    assert result.watch is None
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


# --- D7 étage B : plans armés (EXECUTE_ORDER) ---


def _armed_raw(order: dict | None, **overrides) -> dict:
    raw = {
        "ttl_minutes": 120,
        "on_trigger": "EXECUTE_ORDER",
        "conditions": [
            {"indicator": "z_score", "op": "abs>", "value": 2.0, "interval": "15m", "window": 32}
        ],
        "order": order,
    }
    raw.update(overrides)
    return raw


def _valid_order() -> dict:
    return {
        "intent": "OPEN_SHORT",
        "qty": 50,
        "confidence": 0.85,
        "exit_plan": {"hard_stop": {"type": "price", "price": 88.1}},
        "rationale": "cassure énergie",
    }


def test_execute_order_avec_plan_valide_est_arme() -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    result = build_indicator_watch(_armed_raw(_valid_order()), owner_symbol="CL=F", now=now)

    watch = result.watch
    assert watch is not None
    assert watch["on_trigger"] == "EXECUTE_ORDER"
    order = watch["order"]
    assert order["intent"] == "OPEN_SHORT"
    assert order["action"] == "SELL"  # dérivée de l'intent, pas de mismatch possible
    assert order["qty"] == 50.0
    assert order["confidence"] == 0.85
    assert order["exit_plan"]["hard_stop"]["price"] == 88.1


def test_execute_order_accepte_order_strategy_entry_pine_like() -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    pine_order = {
        "direction": "short",
        "qty": 50,
        "confidence": 0.85,
        "exit": {"id": "bracket", "stop": {"type": "price", "price": 88.1}, "limit": 80.0},
        "rationale": "cassure énergie",
    }

    result = build_indicator_watch(_armed_raw(pine_order), owner_symbol="CL=F", now=now)

    watch = result.watch
    assert watch is not None
    assert watch["on_trigger"] == "EXECUTE_ORDER"
    order = watch["order"]
    assert order["intent"] == "OPEN_SHORT"
    assert order["action"] == "SELL"
    assert order["qty"] == 50.0
    assert order["confidence"] == 0.85
    assert order["exit_plan"] == {
        "hard_stop": {"type": "price", "price": 88.1},
        "take_profits": [{"type": "price", "price": 80.0, "fraction": 1.0, "name": "bracket"}],
    }


def test_execute_order_avec_hard_stop_relatif_est_arme() -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    for hard_stop in (
        {"type": "percent", "percent": 0.025, "min_pct": 0.01, "max_pct": 0.05},
        {"type": "volatility_multiple", "multiple": 1.8, "min_pct": 0.01, "max_pct": 0.05},
        {
            "type": "structural",
            "anchor": "swing_low",
            "window": 20,
            "buffer_pct": 0.001,
        },
    ):
        order = _valid_order()
        order["exit_plan"] = {"hard_stop": hard_stop}

        result = build_indicator_watch(_armed_raw(order), owner_symbol="CL=F", now=now)

        assert result.watch is not None
        assert result.watch["on_trigger"] == "EXECUTE_ORDER"
        assert result.watch["order"]["exit_plan"] == {"hard_stop": hard_stop}
        assert result.rejections == []


def test_execute_order_avec_hard_stop_relatif_malforme_degrade() -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    for hard_stop in (
        {"type": "percent", "percent": 5},
        {"type": "volatility_multiple", "multiple": -1},
        {"type": "structural", "window": 20},
        {"type": "structural", "anchor": "swing_low", "window": 0},
    ):
        order = _valid_order()
        order["exit_plan"] = {"hard_stop": hard_stop}

        result = build_indicator_watch(_armed_raw(order), owner_symbol="CL=F", now=now)

        assert result.watch is not None
        assert result.watch["on_trigger"] == "WAKE_WITH_ORDER_INTENT"
        assert any(r.get("reason") == "invalid_armed_order" for r in result.rejections)


def test_execute_order_sans_hard_stop_degrade_en_wake_with_order_intent() -> None:
    # guardrail à l'armement (fast-fail) : pas de stop -> pas d'exécution directe
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    order = _valid_order()
    order["exit_plan"] = {"take_profits": [{"name": "tp", "price": 80.0, "fraction": 1.0}]}

    result = build_indicator_watch(_armed_raw(order), owner_symbol="CL=F", now=now)

    assert result.watch is not None
    assert result.watch["on_trigger"] == "WAKE_WITH_ORDER_INTENT"  # repasse par le LLM
    assert any(r.get("reason") == "invalid_armed_order" for r in result.rejections)


def test_execute_order_qty_ou_intent_invalides_degrade() -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    bad_qty = _valid_order()
    bad_qty["qty"] = 0
    result = build_indicator_watch(_armed_raw(bad_qty), owner_symbol="CL=F", now=now)
    assert result.watch["on_trigger"] == "WAKE_WITH_ORDER_INTENT"

    bad_intent = _valid_order()
    bad_intent["intent"] = "FLIP"  # seuls OPEN_LONG/OPEN_SHORT sont armables
    result = build_indicator_watch(_armed_raw(bad_intent), owner_symbol="CL=F", now=now)
    assert result.watch["on_trigger"] == "WAKE_WITH_ORDER_INTENT"

    no_order = build_indicator_watch(_armed_raw(None), owner_symbol="CL=F", now=now)
    assert no_order.watch["on_trigger"] == "WAKE_WITH_ORDER_INTENT"


def test_armed_order_price_coherent() -> None:
    from trader.domain.planning.armed_order import armed_order_price_coherent

    short = _valid_order()  # stop à 88.1, SHORT
    assert armed_order_price_coherent(short, price=87.0) is True   # prix sous le stop : ok
    assert armed_order_price_coherent(short, price=88.5) is False  # prix au-delà du stop : incohérent

    long_order = {
        "intent": "OPEN_LONG",
        "qty": 10,
        "exit_plan": {"hard_stop": {"type": "price", "price": 95.0}},
    }
    assert armed_order_price_coherent(long_order, price=100.0) is True
    assert armed_order_price_coherent(long_order, price=94.0) is False


def test_execute_order_ttl_borne_a_la_revue_periodique() -> None:
    # L'expiration de watch est SILENCIEUSE : un TTL court forcerait l'agent à
    # se réveiller pour ré-armer (coût) ou laisserait le scénario désarmé sans
    # qu'il le sache. Cap = 240 min, aligné sur la revue périodique garantie du
    # gate (4 h) : le ré-armement se fait à des réveils qui existent déjà.
    # La fraîcheur est protégée par les checks au déclenchement, pas par l'horloge.
    from datetime import timedelta

    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    result = build_indicator_watch(
        _armed_raw(_valid_order(), ttl_minutes=600), owner_symbol="CL=F", now=now
    )

    expires = datetime.fromisoformat(result.watch["expires_at"])
    assert expires - now == timedelta(minutes=240)  # le cap exact, pas moins

    # une veille simple garde le plafond large (24 h)
    wake = build_indicator_watch(
        _armed_raw(None, ttl_minutes=600, on_trigger="WAKE"), owner_symbol="CL=F", now=now
    )
    expires_wake = datetime.fromisoformat(wake.watch["expires_at"])
    assert expires_wake - now == timedelta(minutes=600)


# --- Phase 2 : normalisation op-aware des labels ---


def test_label_chart_breakout_up_resolu_en_float() -> None:
    """chart_breakout == "breakout_up" -> threshold 1.0."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    result = build_indicator_watch(
        {
            "conditions": [
                {"indicator": "chart_breakout", "op": "==", "value": "breakout_up"}
            ]
        },
        owner_symbol="SPY",
        now=now,
    )
    assert result.watch is not None
    assert result.watch["conditions"][0]["value"] == 1.0


def test_label_candlestick_shooting_star_resolu_en_float() -> None:
    """candlestick_signal == "shooting_star" -> threshold -0.5."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    result = build_indicator_watch(
        {
            "conditions": [
                {"indicator": "candlestick_signal", "op": "==", "value": "shooting_star"}
            ]
        },
        owner_symbol="SPY",
        now=now,
    )
    assert result.watch is not None
    assert result.watch["conditions"][0]["value"] == -0.5


def test_label_candlestick_shooting_star_abs_op_resolu_positif() -> None:
    """candlestick_signal abs>= "shooting_star" -> seuil abs(−0.5) = 0.5."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    result = build_indicator_watch(
        {
            "conditions": [
                {"indicator": "candlestick_signal", "op": "abs>=", "value": "shooting_star"}
            ]
        },
        owner_symbol="SPY",
        now=now,
    )
    assert result.watch is not None
    assert result.watch["conditions"][0]["value"] == 0.5


def test_seuil_numerique_negatif_abs_op_pris_en_magnitude() -> None:
    """Un seuil NUMÉRIQUE négatif avec un op abs* devient sa magnitude, sinon
    `abs(actual) >= -0.5` serait toujours vrai (prédicat permissif → réveil-fantôme)."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    result = build_indicator_watch(
        {
            "conditions": [
                {"indicator": "candlestick_signal", "op": "abs>=", "value": -0.5}
            ]
        },
        owner_symbol="SPY",
        now=now,
    )
    assert result.watch is not None
    assert result.watch["conditions"][0]["value"] == 0.5


def test_label_inconnu_rejet_avec_valid_labels() -> None:
    """Label "shooting_starr" inconnu -> rejet unknown_indicator_label + valid_labels."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    result = build_indicator_watch(
        {
            "conditions": [
                {"indicator": "candlestick_signal", "op": "==", "value": "shooting_starr"}
            ]
        },
        owner_symbol="SPY",
        now=now,
    )
    assert result.watch is None
    assert len(result.rejections) == 1
    rejection = result.rejections[0]
    assert rejection["reason"] == WATCH_REJECT_UNKNOWN_LABEL
    assert rejection["raw_value"] == "shooting_starr"
    # valid_labels dérivé de INDICATOR_LABEL_VALUES, contient la bonne variante
    assert "shooting_star" in rejection["valid_labels"]
    assert "bullish_engulfing" in rejection["valid_labels"]


def test_non_regression_label_float_evaluable() -> None:
    """chart_breakout == 1.0 (float direct) -> watch évaluable sans régression."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    result = build_indicator_watch(
        {
            "conditions": [
                {"indicator": "chart_breakout", "op": "==", "value": 1.0}
            ]
        },
        owner_symbol="SPY",
        now=now,
    )
    assert result.watch is not None
    assert result.watch["conditions"][0]["value"] == 1.0


# --- Phase 3 : filet atomique ---


def test_filet_atomique_logic_all_une_condition_rejetee() -> None:
    """logic=all : 1 condition valide + 1 rejetée -> watch is None, rejets exposés."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    result = build_indicator_watch(
        {
            "logic": "all",
            "conditions": [
                {"indicator": "z_score", "op": ">=", "value": 1.5},
                {"indicator": "return", "op": ">", "value": None},
            ],
        },
        owner_symbol="SPY",
        now=now,
    )
    assert result.watch is None
    assert len(result.rejections) >= 1
    assert result.rejections[0]["reason"] == WATCH_REJECT_NON_FINITE_THRESHOLD


def test_filet_atomique_logic_any_une_condition_rejetee() -> None:
    """logic=any : 1 condition valide + 1 rejetée -> watch is None, rejets exposés."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    result = build_indicator_watch(
        {
            "logic": "any",
            "conditions": [
                {"indicator": "z_score", "op": ">=", "value": 1.5},
                {"indicator": "return", "op": ">", "value": "NaN"},
            ],
        },
        owner_symbol="SPY",
        now=now,
    )
    assert result.watch is None
    assert len(result.rejections) >= 1


def test_filet_atomique_execute_order_condition_rejetee() -> None:
    """Ordre armé valide + 1 condition rejetée -> watch is None (jamais EXECUTE_ORDER ni WAKE_WITH_ORDER_INTENT)."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    raw = {
        "on_trigger": "EXECUTE_ORDER",
        "conditions": [
            {"indicator": "z_score", "op": "abs>", "value": 2.0},
            {"indicator": "return", "op": ">", "value": None},
        ],
        "order": _valid_order(),
    }
    result = build_indicator_watch(raw, owner_symbol="CL=F", now=now)
    assert result.watch is None
    assert len(result.rejections) >= 1


def test_non_regression_execute_order_valide_conditions_ok_sans_hard_stop() -> None:
    """EXECUTE_ORDER conditions valides + hard_stop manquant -> toujours WAKE_WITH_ORDER_INTENT."""
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    order = _valid_order()
    order["exit_plan"] = {"take_profits": [{"name": "tp", "price": 80.0, "fraction": 1.0}]}
    result = build_indicator_watch(_armed_raw(order), owner_symbol="CL=F", now=now)
    assert result.watch is not None
    assert result.watch["on_trigger"] == "WAKE_WITH_ORDER_INTENT"
    assert any(r.get("reason") == "invalid_armed_order" for r in result.rejections)
