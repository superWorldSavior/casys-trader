"""D8 étage 1 — replayer mécanique de plans armés (évaluation a posteriori)."""

from datetime import datetime, timedelta, timezone

from backtest.plan_replay import replay_armed_plan
from trader.tools.market import Bar

_T0 = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)


def _bars(closes: list[float], *, spread: float = 0.5) -> list[Bar]:
    return [
        Bar(
            ts=(_T0 + timedelta(minutes=15 * i)).isoformat(),
            open=c,
            high=c + spread,
            low=c - spread,
            close=c,
            volume=1000.0,
        )
        for i, c in enumerate(closes)
    ]


def _plan(*, stop: float, take_profit: float | None = None, intent: str = "OPEN_LONG",
          trigger_value: float = 0.005) -> dict:  # 0.005 = +0,5 % (return est une fraction)
    exit_plan: dict = {"hard_stop": {"type": "price", "price": stop}}
    if take_profit is not None:
        exit_plan["take_profits"] = [{"name": "tp1", "price": take_profit, "fraction": 1.0}]
    return {
        "id": "SPY:replay01",
        "symbol": "SPY",
        "created_at": _T0.isoformat(),
        "expires_at": (_T0 + timedelta(hours=4)).isoformat(),
        "logic": "all",
        "on_trigger": "EXECUTE_ORDER",
        # déclenche quand le return fractionnel (fenêtre de 4 barres) dépasse trigger_value
        "conditions": [
            {
                "symbol": "SPY",
                "indicator": "return",
                "op": ">",
                "value": trigger_value,
                "interval": "15m",
                "timeframe": "15m",
                "source_interval": "15m",
                "lookback": "5d",
                "window": 4,
                "as_of": "latest",
            }
        ],
        "order": {
            "intent": intent,
            "action": "BUY" if intent == "OPEN_LONG" else "SELL",
            "qty": 10.0,
            "confidence": 0.9,
            "exit_plan": exit_plan,
        },
    }


def test_plan_declenche_puis_take_profit() -> None:
    # hausse franche -> trigger ; puis le TP à 110 est touché
    closes = [100.0, 100.2, 100.4, 102.0, 104.0, 107.0, 111.0, 112.0]
    result = replay_armed_plan(_plan(stop=95.0, take_profit=110.0), _bars(closes))

    assert result.status == "executed"
    assert result.triggered_at is not None
    assert result.exit_reason is not None and "take_profit" in result.exit_reason
    assert result.pnl_pct is not None and result.pnl_pct > 0


def test_plan_jamais_declenche_expire() -> None:
    closes = [100.0] * 8  # plat : return ~0, jamais > 0.5
    result = replay_armed_plan(_plan(stop=95.0), _bars(closes))

    assert result.status == "expired"
    assert result.triggered_at is None
    assert result.pnl_pct is None


def test_plan_declenche_avec_prix_deja_sous_le_stop_est_annule() -> None:
    # SHORT avec stop à 99 : au moment du trigger le prix (~104) est AU-DESSUS
    # du stop -> incohérent, annulation (même check qu'en live)
    closes = [100.0, 100.2, 100.4, 102.0, 104.0]
    result = replay_armed_plan(
        _plan(stop=99.0, intent="OPEN_SHORT"), _bars(closes)
    )

    assert result.status == "cancelled:stop_incoherent"
    assert result.pnl_pct is None


def test_plan_declenche_puis_stoppe_en_perte() -> None:
    # trigger sur la hausse, puis retournement qui traverse le stop à 101
    closes = [100.0, 100.5, 101.0, 103.0, 104.0, 102.0, 100.5, 99.0]
    result = replay_armed_plan(_plan(stop=101.0), _bars(closes))

    assert result.status == "executed"
    assert result.exit_reason == "hard_stop"
    assert result.pnl_pct is not None and result.pnl_pct < 0


def test_donnees_finies_avant_la_sortie_clot_a_l_horizon() -> None:
    # trigger puis plus assez de barres pour toucher stop (90) ou TP (120)
    closes = [100.0, 100.5, 102.0, 103.0, 103.5]
    result = replay_armed_plan(_plan(stop=90.0, take_profit=120.0), _bars(closes))

    assert result.status == "executed"
    assert result.exit_reason == "horizon"
    assert result.exit_price == 103.5
