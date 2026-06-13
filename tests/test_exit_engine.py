from datetime import datetime, timezone

import pytest

import trader.exit_engine as exit_engine
from trader.exit_engine import evaluate_plan
from trader.trade_plan import create_trade_plan, trade_plan_from_dict


def _plan():
    return create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": {"type": "price", "price": 95.0},
            "take_profits": [
                {"name": "tp1", "price": 105.0, "fraction": 0.5, "after_fill": "move_stop_to_breakeven"},
                {"name": "tp2", "price": 110.0, "fraction": 0.5, "after_fill": "close"},
            ],
            "trailing_stop": {"enabled_after": "tp1", "trail_type": "price", "trail_value": 2.0},
            "max_hold_minutes": 45,
        },
    )


def test_evaluate_plan_declenche_tp1_et_deplace_stop_a_breakeven() -> None:
    result = evaluate_plan(
        _plan(),
        price=106.0,
        now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
    )

    assert result.signal is not None
    assert result.signal.reason == "take_profit:tp1"
    assert result.signal.quantity == 5.0
    assert result.updated_plan.remaining_quantity == 5.0
    assert result.updated_plan.hard_stop_price == 100.0
    assert "tp1" in result.updated_plan.filled_take_profits


def test_evaluate_plan_after_fill_close_cloture_toute_la_quantite_restante() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "take_profits": [
                {"name": "tp1", "price": 105.0, "fraction": 0.5, "after_fill": "close"},
            ],
        },
    )

    result = evaluate_plan(
        plan,
        price=106.0,
        now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
    )

    assert result.signal is not None
    assert result.signal.reason == "take_profit:tp1"
    assert result.signal.quantity == 10.0
    assert result.updated_plan.remaining_quantity == 0.0
    assert result.close_plan is True


def test_evaluate_plan_declenche_trailing_stop_apres_tp1() -> None:
    plan = _plan()
    first = evaluate_plan(plan, price=106.0, now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc))
    second = evaluate_plan(
        first.updated_plan,
        price=104.0,
        now=datetime(2026, 6, 5, 12, 15, tzinfo=timezone.utc),
    )

    assert second.signal is not None
    assert second.signal.reason == "trailing_stop"
    assert second.signal.quantity == 5.0
    assert second.updated_plan.remaining_quantity == 0.0
    assert second.close_plan is True


def test_evaluate_plan_declenche_max_hold() -> None:
    result = evaluate_plan(
        _plan(),
        price=101.0,
        now=datetime(2026, 6, 5, 12, 46, tzinfo=timezone.utc),
    )

    assert result.signal is not None
    assert result.signal.reason == "max_hold"
    assert result.signal.quantity == 10.0
    assert result.close_plan is True


def test_evaluate_plan_protege_un_short_apres_gain_puis_giveback() -> None:
    plan = create_trade_plan(
        symbol="NVDA",
        side="SHORT",
        quantity=48.0,
        entry_price=207.74,
        opened_at="2026-06-05T17:51:12+00:00",
        raw_exit_plan={
            "hard_stop": 213.50,
            "take_profits": [{"name": "tp1", "price": 201.0, "fraction": 0.5}],
            "profit_protection": True,
        },
    )

    armed = evaluate_plan(
        plan,
        price=204.74,
        now=datetime(2026, 6, 5, 18, 10, tzinfo=timezone.utc),
    )
    protected = evaluate_plan(
        armed.updated_plan,
        price=205.96,
        now=datetime(2026, 6, 5, 18, 20, tzinfo=timezone.utc),
    )

    assert armed.signal is None
    assert protected.signal is not None
    assert protected.signal.reason == "profit_protection"
    assert protected.signal.side == "BUY"
    assert protected.signal.quantity == 16.0
    assert protected.updated_plan.remaining_quantity == 32.0
    assert protected.updated_plan.hard_stop_price == 207.74
    assert protected.updated_plan.profit_protection is not None
    assert protected.updated_plan.profit_protection.triggered is True


def test_evaluate_plan_ne_protege_pas_deux_fois() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=9.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "take_profits": [{"price": 110.0, "fraction": 1.0}],
            "profit_protection": True,
        },
    )

    armed = evaluate_plan(plan, price=103.0, now=datetime(2026, 6, 5, 12, 20, tzinfo=timezone.utc))
    protected = evaluate_plan(armed.updated_plan, price=101.5, now=datetime(2026, 6, 5, 12, 25, tzinfo=timezone.utc))
    again = evaluate_plan(protected.updated_plan, price=101.0, now=datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc))

    assert protected.signal is not None
    assert protected.signal.reason == "profit_protection"
    assert protected.signal.quantity == 3.0
    assert again.signal is None


def test_create_plan_ne_met_pas_protection_defaut_si_trailing_existe() -> None:
    assert _plan().profit_protection is None


def test_create_plan_ne_met_pas_protection_defaut_sans_demande_agent() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"hard_stop": 95.0},
    )

    assert plan.profit_protection is None


def test_trailing_volatility_multiple_utilise_la_volatilite_reference() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"trailing_stop": {"trail_type": "volatility_multiple", "trail_value": 2.0}},
        reference_volatility=1.25,
    )

    assert exit_engine._trail_amount(plan) == pytest.approx(2.5)


# ── Chantier A : stops/TP/trailing évalués sur le high/low de barre ───────────


def _short_plan_clf():
    """Plan short CL=F du post-mortem : short 86.71 stop 87.30."""
    return create_trade_plan(
        symbol="CL=F",
        side="SHORT",
        quantity=115.0,
        entry_price=86.71,
        opened_at="2026-06-09T16:09:45+00:00",
        raw_exit_plan={"hard_stop": 87.30},
    )


def _now():
    return datetime(2026, 6, 9, 16, 30, tzinfo=timezone.utc)


class TestHardStopIntraBar:
    """Hard stop évalué sur bar_high (SHORT) ou bar_low (LONG)."""

    def test_short_stop_declenche_par_bar_high_meme_si_price_sous_stop(self) -> None:
        # Cas spike-revenu : price < stop mais bar_high a traversé le stop.
        # Stop 87.30, price close 86.50, bar_high 89.49 → déclenché, fill = stop
        plan = _short_plan_clf()
        result = evaluate_plan(plan, price=86.50, bar_high=89.49, now=_now())
        assert result.signal is not None
        assert result.signal.reason == "hard_stop"
        assert result.signal.fill_price == pytest.approx(87.30)  # fill conservateur = stop

    def test_short_stop_declenche_par_price_au_dessus_stop(self) -> None:
        # Cas réel post-mortem : price 87.97, bar_high 89.49 → déclenché, fill = price
        plan = _short_plan_clf()
        result = evaluate_plan(plan, price=87.97, bar_high=89.49, now=_now())
        assert result.signal is not None
        assert result.signal.reason == "hard_stop"
        assert result.signal.fill_price == pytest.approx(87.97)  # price > stop

    def test_long_stop_declenche_par_bar_low(self) -> None:
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"hard_stop": 95.0},
        )
        # price revenu au-dessus du stop mais bar_low a traversé
        result = evaluate_plan(plan, price=96.0, bar_low=93.0, now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc))
        assert result.signal is not None
        assert result.signal.reason == "hard_stop"
        assert result.signal.fill_price == pytest.approx(95.0)  # fill conservateur = stop

    def test_long_stop_fill_price_est_price_quand_price_sous_stop(self) -> None:
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"hard_stop": 95.0},
        )
        # price 94.0 < stop 95.0 → fill = price (pire que le stop)
        result = evaluate_plan(plan, price=94.0, bar_low=93.0, now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc))
        assert result.signal is not None
        assert result.signal.reason == "hard_stop"
        assert result.signal.fill_price == pytest.approx(94.0)

    def test_bar_high_low_none_comportement_inchange(self) -> None:
        # Non-régression : sans bar_high/bar_low → identique au comportement actuel
        plan = _short_plan_clf()
        # price < stop → pas de déclenchement sans bar_high
        result = evaluate_plan(plan, price=86.50, now=_now())
        assert result.signal is None

    def test_short_stop_non_declenche_si_bar_high_sous_stop(self) -> None:
        plan = _short_plan_clf()
        # bar_high 87.10 < stop 87.30 → pas de déclenchement
        result = evaluate_plan(plan, price=86.50, bar_high=87.10, now=_now())
        assert result.signal is None


class TestTakeProfitIntraBar:
    """TPs évalués sur les extrêmes de barre (sémantique limit)."""

    def test_long_tp_declenche_par_bar_high_price_revenu_dessous(self) -> None:
        # TP LONG à 105, bar_high 106, price close 103 → déclenché, fill = tp.price
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
        )
        result = evaluate_plan(
            plan,
            price=103.0,
            bar_high=106.0,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        assert result.signal is not None
        assert result.signal.reason == "take_profit:tp1"
        assert result.signal.fill_price == pytest.approx(105.0)  # fill au niveau du TP

    def test_long_tp_fill_prix_courant_si_plus_favorable(self) -> None:
        # price 107 > tp 105 → fill au prix courant (meilleur)
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
        )
        result = evaluate_plan(
            plan,
            price=107.0,
            bar_high=108.0,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        assert result.signal is not None
        assert result.signal.fill_price == pytest.approx(107.0)

    def test_short_tp_declenche_par_bar_low(self) -> None:
        plan = create_trade_plan(
            symbol="CL=F",
            side="SHORT",
            quantity=10.0,
            entry_price=90.0,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 85.0, "fraction": 0.5}]},
        )
        # bar_low 84.0 < tp 85.0 → déclenché, price close revenu à 86.0 → fill = tp.price
        result = evaluate_plan(
            plan,
            price=86.0,
            bar_low=84.0,
            now=datetime(2026, 6, 9, 17, 0, tzinfo=timezone.utc),
        )
        assert result.signal is not None
        assert result.signal.reason == "take_profit:tp1"
        assert result.signal.fill_price == pytest.approx(85.0)

    def test_tp_non_declenche_sans_bar_extremes(self) -> None:
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
        )
        # price 103.0 et pas de bar_high → pas de déclenchement
        result = evaluate_plan(plan, price=103.0, now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc))
        assert result.signal is None


class TestWatermarkIntraBar:
    """Les watermarks avancent aussi sur les extrêmes de barre."""

    def test_watermark_avance_sur_bar_high_pour_long(self) -> None:
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "price", "trail_value": 2.0},
            },
        )
        # price close = 101, bar_high = 106 → watermark = 106.
        # L'armement par défaut n'a pas le droit de déclencher sur cette même barre.
        result = evaluate_plan(
            plan,
            price=101.0,
            bar_high=106.0,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        assert result.signal is None
        assert result.updated_plan.high_watermark == pytest.approx(106.0)

    def test_long_trailing_declenche_sur_barre_suivante_apres_watermark_bar_high(self) -> None:
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "price", "trail_value": 2.0},
            },
        )
        armed = evaluate_plan(
            plan,
            price=101.0,
            bar_high=106.0,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        result = evaluate_plan(
            armed.updated_plan,
            price=100.5,
            bar_low=99.5,
            now=datetime(2026, 6, 5, 12, 15, tzinfo=timezone.utc),
        )
        assert armed.signal is None
        assert result.signal is not None
        assert result.signal.reason == "trailing_stop"
        assert result.signal.fill_price is not None
        assert result.signal.fill_price >= plan.entry_price

    def test_watermark_low_avance_sur_bar_low_pour_short(self) -> None:
        plan = create_trade_plan(
            symbol="CL=F",
            side="SHORT",
            quantity=10.0,
            entry_price=90.0,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "price", "trail_value": 2.0},
            },
        )
        # bar_low 84.0 → watermark_low = 84, sans déclenchement sur la barre d'armement.
        result = evaluate_plan(
            plan,
            price=86.5,
            bar_low=84.0,
            now=datetime(2026, 6, 9, 17, 0, tzinfo=timezone.utc),
        )
        assert result.signal is None
        assert result.updated_plan.low_watermark == pytest.approx(84.0)

    def test_short_trailing_declenche_sur_barre_suivante_apres_watermark_bar_low(self) -> None:
        plan = create_trade_plan(
            symbol="CL=F",
            side="SHORT",
            quantity=10.0,
            entry_price=90.0,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "price", "trail_value": 2.0},
            },
        )
        armed = evaluate_plan(
            plan,
            price=86.5,
            bar_low=84.0,
            now=datetime(2026, 6, 9, 17, 0, tzinfo=timezone.utc),
        )
        result = evaluate_plan(
            armed.updated_plan,
            price=89.5,
            bar_high=90.5,
            now=datetime(2026, 6, 9, 17, 5, tzinfo=timezone.utc),
        )
        assert armed.signal is None
        assert result.signal is not None
        assert result.signal.reason == "trailing_stop"
        assert result.signal.fill_price is not None
        assert result.signal.fill_price <= plan.entry_price


class TestStopPrioriteOnTP:
    """Stop prioritaire sur TP quand les deux sont croisés dans la même barre."""

    def test_stop_prioritaire_sur_tp_si_les_deux_croises(self) -> None:
        # Long : bar_low 93 < stop 95 et bar_high 107 > tp1 105 → stop gagne
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "hard_stop": 95.0,
                "take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}],
            },
        )
        result = evaluate_plan(
            plan,
            price=101.0,
            bar_high=107.0,
            bar_low=93.0,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        assert result.signal is not None
        assert result.signal.reason == "hard_stop"


class TestFillPriceSurExitsSansBarExtremes:
    """fill_price sur les exits classiques (sans bar_high/bar_low) pour non-régression."""

    def test_hard_stop_fill_price_est_price_sans_bar_extremes(self) -> None:
        plan = _short_plan_clf()
        # price > stop → déclenchement, fill_price = price
        result = evaluate_plan(plan, price=87.50, now=_now())
        assert result.signal is not None
        assert result.signal.reason == "hard_stop"
        assert result.signal.fill_price == pytest.approx(87.50)

    def test_tp_fill_price_est_price_sans_bar_extremes(self) -> None:
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
        )
        result = evaluate_plan(plan, price=106.0, now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc))
        assert result.signal is not None
        assert result.signal.fill_price == pytest.approx(106.0)


# ── Review Codex — 5 corrections ──────────────────────────────────────────────


class TestTrailingStopIntraBar:
    """MAJOR 1 — trailing stop doit être évalué sur bar_low/bar_high (cohérence avec hard stop)."""

    def test_short_trailing_par_defaut_attend_un_gain_au_moins_egal_au_trail(self) -> None:
        plan = create_trade_plan(
            symbol="GC=F",
            side="SHORT",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={"trailing_stop": {"trail_type": "price", "trail_value": 2.0}},
        )

        result = evaluate_plan(
            plan,
            price=100.8,
            bar_high=101.2,
            bar_low=99.0,
            now=datetime(2026, 6, 9, 16, 10, tzinfo=timezone.utc),
        )

        assert result.signal is None
        assert result.updated_plan.low_watermark == pytest.approx(99.0)

    def test_short_trailing_par_defaut_arme_sans_declencher_sur_la_meme_barre(self) -> None:
        plan = create_trade_plan(
            symbol="GC=F",
            side="SHORT",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={"trailing_stop": {"trail_type": "price", "trail_value": 2.0}},
        )

        result = evaluate_plan(
            plan,
            price=99.8,
            bar_high=100.1,
            bar_low=98.0,
            now=datetime(2026, 6, 9, 16, 10, tzinfo=timezone.utc),
        )

        assert result.signal is None
        assert result.updated_plan.low_watermark == pytest.approx(98.0)

    def test_trailing_enabled_after_tp_reste_declenchable_sans_gain_egal_au_trail(self) -> None:
        plan = create_trade_plan(
            symbol="GC=F",
            side="SHORT",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={
                "take_profits": [{"name": "tp1", "price": 99.5, "fraction": 0.5}],
                "trailing_stop": {"enabled_after": "tp1", "trail_type": "price", "trail_value": 2.0},
            },
        )
        filled = evaluate_plan(
            plan,
            price=99.5,
            now=datetime(2026, 6, 9, 16, 10, tzinfo=timezone.utc),
        )

        result = evaluate_plan(
            filled.updated_plan,
            price=101.2,
            bar_high=101.6,
            now=datetime(2026, 6, 9, 16, 15, tzinfo=timezone.utc),
        )

        assert result.signal is not None
        assert result.signal.reason == "trailing_stop"

    def test_long_trailing_declenche_par_bar_low_price_revenu_au_dessus(self) -> None:
        # LONG, trail = 2.0. Watermark initiale = entry 100.
        # bar_high = 106 → watermark devient 106.
        # bar_low = 103.5 < 106 - 2 = 104, mais la même barre ne peut qu'armer.
        # price close = 105 (revenu au-dessus du niveau trailing 104).
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"trailing_stop": {"trail_type": "price", "trail_value": 2.0}},
        )
        result = evaluate_plan(
            plan,
            price=105.0,
            bar_high=106.0,
            bar_low=103.5,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        assert result.signal is None
        assert result.updated_plan.high_watermark == pytest.approx(106.0)

    def test_long_trailing_par_defaut_ne_sort_pas_a_perte_sur_barre_d_armement(self) -> None:
        # bar_high arme le trailing à breakeven, mais bar_low/price sous le niveau ne sortent pas sur cette barre.
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"trailing_stop": {"trail_type": "price", "trail_value": 2.0}},
        )
        result = evaluate_plan(
            plan,
            price=103.0,
            bar_high=106.0,
            bar_low=103.0,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        assert result.signal is None

    def test_short_trailing_declenche_par_bar_high_price_revenu_dessous(self) -> None:
        # SHORT, trail = 2.0. Watermark_low initiale = entry 90.
        # bar_low = 84 → watermark_low = 84.
        # bar_high = 86.5 > 84 + 2 = 86, mais la même barre ne peut qu'armer.
        # price close = 85.5 (revenu sous le niveau trailing 86).
        plan = create_trade_plan(
            symbol="CL=F",
            side="SHORT",
            quantity=10.0,
            entry_price=90.0,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={"trailing_stop": {"trail_type": "price", "trail_value": 2.0}},
        )
        result = evaluate_plan(
            plan,
            price=85.5,
            bar_high=86.5,
            bar_low=84.0,
            now=datetime(2026, 6, 9, 17, 0, tzinfo=timezone.utc),
        )
        assert result.signal is None
        assert result.updated_plan.low_watermark == pytest.approx(84.0)

    def test_trailing_non_declenche_si_bar_low_au_dessus_niveau(self) -> None:
        # bar_high 106 → watermark 106, niveau trailing = 104.
        # bar_low 104.5 > 104 → pas de déclenchement
        plan = create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"trailing_stop": {"trail_type": "price", "trail_value": 2.0}},
        )
        result = evaluate_plan(
            plan,
            price=105.0,
            bar_high=106.0,
            bar_low=104.5,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        assert result.signal is None


class TestTrailingVolatilityLegacy:
    """Plan legacy volatility_multiple sans reference_volatility : trailing inactif, pas de crash."""

    def _plan(self, **overrides):
        raw = {
            "id": "SPY-legacy-vol-trail",
            "symbol": "SPY",
            "side": "LONG",
            "quantity": 10.0,
            "remaining_quantity": 10.0,
            "entry_price": 100.0,
            "opened_at": "2026-06-05T12:00:00+00:00",
            "reference_volatility": None,
            "hard_stop_price": None,
            "take_profits": [],
            "trailing_stop": {
                "enabled_after": None,
                "trail_type": "volatility_multiple",
                "trail_value": 2.0,
            },
            "max_hold_minutes": None,
            "high_watermark": 105.0,
            "low_watermark": 100.0,
            "filled_take_profits": [],
        }
        raw.update(overrides)
        return trade_plan_from_dict(raw)

    def test_volatility_multiple_sans_reference_rechargee_ne_leve_pas(self) -> None:
        plan = self._plan()

        result = evaluate_plan(
            plan,
            price=103.0,
            bar_low=102.0,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )

        assert result.signal is None
        assert result.updated_plan.high_watermark == pytest.approx(105.0)

    def test_hard_stop_continue_de_proteger_un_plan_volatility_multiple_legacy(self) -> None:
        plan = self._plan(hard_stop_price=99.0)

        result = evaluate_plan(
            plan,
            price=98.5,
            now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
        )

        assert result.signal is not None
        assert result.signal.reason == "hard_stop"

    def test_max_hold_continue_de_proteger_un_plan_volatility_multiple_legacy(self) -> None:
        plan = self._plan(max_hold_minutes=5.0)

        result = evaluate_plan(
            plan,
            price=103.0,
            now=datetime(2026, 6, 5, 12, 5, tzinfo=timezone.utc),
        )

        assert result.signal is not None
        assert result.signal.reason == "max_hold"


class TestProfitProtectionFillPrice:
    """MINOR 4 — profit_protection doit porter fill_price=price explicite."""

    def test_profit_protection_signal_porte_fill_price(self) -> None:
        plan = create_trade_plan(
            symbol="NVDA",
            side="SHORT",
            quantity=48.0,
            entry_price=207.74,
            opened_at="2026-06-05T17:51:12+00:00",
            raw_exit_plan={
                "hard_stop": 213.50,
                "take_profits": [{"name": "tp1", "price": 201.0, "fraction": 0.5}],
                "profit_protection": True,
            },
        )
        # Premier appel pour armer la protection (watermark baissier pour short)
        armed = evaluate_plan(
            plan,
            price=204.74,
            now=datetime(2026, 6, 5, 18, 10, tzinfo=timezone.utc),
        )
        # Deuxième appel : giveback → protection déclenchée
        protected = evaluate_plan(
            armed.updated_plan,
            price=205.96,
            now=datetime(2026, 6, 5, 18, 20, tzinfo=timezone.utc),
        )
        assert protected.signal is not None
        assert protected.signal.reason == "profit_protection"
        # fill_price doit être le prix courant, pas None
        assert protected.signal.fill_price == pytest.approx(205.96)
