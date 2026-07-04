"""__main__ — CLI du backtest maison.

Câble data + engine + metrics + le brain Codex. Rejeu ÉCHANTILLONNÉ pour borner
le coût (un appel Codex par pas × symbole). Mode `--mock` pour valider le plumbing
sans appeler Codex.

Exemples :
    uv run python -m backtest --mock --days 30 --sample-every 5
    uv run python -m backtest --days 30 --sample-every 7 --max-steps 5 --symbols SPY QQQ
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from trader.agent import client as codex_client
from trader.agent import memory as agent_memory
from trader.execution.broker import commission_model_from_name

from .data import DataError, HistoryStore
from .engine import run_backtest
from .metrics import compute_metrics, render_cli

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

# yfinance ne sert l'intraday (1h) que ~730 jours en arrière.
_INTRADAY_MAX_DAYS = 729

CAVEAT = (
    "⚠️  Ce que ce backtest NE mesure PAS (à ne pas surinterpréter) :\n"
    "   - fills parfaits (SimBroker : pas de slippage ; frais IBKR estimés si activés)\n"
    "   - data leakage possible : le LLM a pu voir cet historique à l'entraînement\n"
    "   - rejeu échantillonné (pas barre par barre)\n"
    "   Pour un agent adaptatif, le forward paper reste l'évaluation de référence."
)


def _mock_decision_fn(symbol: str, context: dict):
    """Décision déterministe sans Codex : entre en position si à plat, sinon HOLD.

    Sert uniquement à valider le câblage data→engine→metrics (zéro coût LLM)."""
    positions = context["portfolio"]["positions"]
    held = positions.get(symbol, {}).get("quantity", 0)
    if held:
        return codex_client.Decision.hold(symbol, "déjà en position (mock)")
    price = context["price"] or 1.0
    qty = max(1.0, round(1000.0 / price))  # ~1000 $ de notionnel
    return codex_client.Decision(symbol, "BUY", qty, 0.5, "entrée mock")


def _codex_decision_fn(mandate: str, memory: str, model: str):
    """Wrap le vrai brain Codex en DecisionFn (symbol, context) -> Decision."""

    def fn(symbol: str, context: dict):
        return codex_client.decide(
            symbol=symbol, mandate=mandate, memory=memory, context=context, model=model
        )

    return fn


def _default_range(days: int) -> tuple[str, str]:
    """Fenêtre [start, end] ; end = hier (barres closes), bornée à la limite intraday."""
    if days > _INTRADAY_MAX_DAYS:
        days = _INTRADAY_MAX_DAYS
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest maison de l'agent (SimBroker + yfinance + Codex)")
    parser.add_argument("--days", type=int, default=30, help="profondeur d'historique en jours (borné à 729 pour 1h)")
    parser.add_argument("--interval", default="1h", help="résolution yfinance (1h, 1d, …)")
    parser.add_argument("--symbols", nargs="*", help="override de l'univers (défaut: config/universe.yaml)")
    parser.add_argument("--sample-every", type=int, default=5, help="ne garder qu'un pas sur N (borne le coût)")
    parser.add_argument("--max-steps", type=int, default=0, help="cap dur sur le nombre de pas rejoués (0 = pas de cap)")
    parser.add_argument("--lookback", type=int, default=50, help="nb de barres passées fournies à la décision")
    parser.add_argument("--mock", action="store_true", help="décision déterministe sans Codex (test plumbing)")
    parser.add_argument("--model", default=codex_client.DEFAULT_MODEL, help="modèle Codex pour les décisions")
    parser.add_argument(
        "--commission-model",
        default=os.getenv("TRADER_COMMISSION_MODEL", "ibkr"),
        choices=["none", "ibkr"],
        help="modèle de frais SimBroker (défaut/env TRADER_COMMISSION_MODEL: ibkr)",
    )
    args = parser.parse_args()
    commission_model = commission_model_from_name(args.commission_model)

    universe_cfg = yaml.safe_load((ROOT / "config" / "universe.yaml").read_text())
    risk_cfg = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    symbols = args.symbols or universe_cfg["symbols"]
    starting_cash = float(universe_cfg.get("starting_cash", 100_000))

    start, end = _default_range(args.days)
    print(f"Chargement {symbols} de {start} à {end} (interval={args.interval})…")
    try:
        history = HistoryStore.load(symbols, start, end, interval=args.interval)
    except DataError as e:
        print(f"ERREUR data [{e.code}] : {e.context}")
        raise SystemExit(1)

    timeline = history.timeline()
    sampled = timeline[:: max(1, args.sample_every)]
    if args.max_steps > 0:
        sampled = sampled[-args.max_steps :]

    n_calls = 0 if args.mock else len(sampled) * len(symbols)
    print(f"Timeline: {len(timeline)} pas → {len(sampled)} échantillonnés. "
          f"{'MOCK (0 appel Codex)' if args.mock else f'~{n_calls} appels Codex ({args.model})'}.")

    if args.mock:
        decision_fn = _mock_decision_fn
    else:
        mem = agent_memory.Memory(ROOT / "mandate" / "mandate.md", ROOT / "mandate" / "memory.md")
        decision_fn = _codex_decision_fn(mem.read_mandate(), mem.read_memory(), args.model)

    result = run_backtest(
        history=history,
        timeline=sampled,
        symbols=symbols,
        decision_fn=decision_fn,
        risk_limits=risk_cfg,
        starting_cash=starting_cash,
        lookback=args.lookback,
        commission_model=commission_model,
    )

    metrics = compute_metrics(result.equity_curve, result.trades, result.starting_equity)
    print()
    print(render_cli(metrics))
    print()
    print(CAVEAT)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = STATE_DIR / "last_backtest.json"
    out.write_text(json.dumps(
        {
            "range": [start, end],
            "interval": args.interval,
            "symbols": symbols,
            "sampled_steps": len(sampled),
            "mock": args.mock,
            "commission_model": args.commission_model,
            "metrics": metrics.__dict__,
            "trades": result.trades,
            "equity_curve": result.equity_curve,
        },
        indent=2, ensure_ascii=False,
    ))
    print(f"\nRapport complet → {out}")


if __name__ == "__main__":
    main()
