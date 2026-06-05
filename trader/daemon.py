"""daemon — la boucle runtime (boucle 2).

Un cycle = réveil -> contexte (marché + portefeuille) -> Codex décide ->
risk gate -> exécution paper -> log -> l'agent planifie son prochain réveil.

L'infra orchestre ; la STRATÉGIE n'est pas ici. Le daemon ne fait que :
  câbler les outils, appeler Codex, faire respecter le fusible, exécuter, logger.

Safe defaults : `--dry-run` par défaut (n'exécute pas, log seulement). Kill switch
via fichier. Toute erreur Codex -> HOLD.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import codex_client
from .risk import RiskGate, RiskLimits
from .tools import market, memory as memory_mod, portfolio, scheduler
from .tools.execution import Order, SimBroker

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

log = logging.getLogger("casys-trader")


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _kill_switch_active() -> bool:
    return (ROOT / "KILL").exists()


def _compact_bars(bars: list, n: int = 48) -> list[dict]:
    """Compacte les n dernières barres pour le contexte de décision (clés courtes)."""
    return [
        {"t": b.ts, "o": round(b.open, 4), "h": round(b.high, 4),
         "l": round(b.low, 4), "c": round(b.close, 4), "v": round(b.volume, 2)}
        for b in bars[-n:]
    ]


def run_cycle(*, dry_run: bool, now: datetime | None = None) -> dict:
    """Exécute UN cycle. Retourne un rapport structuré (machine-readable)."""
    now = now or datetime.now(timezone.utc)
    universe_cfg = _load_yaml(ROOT / "config" / "universe.yaml")
    risk_cfg = _load_yaml(ROOT / "config" / "risk.yaml")

    symbols: list[str] = universe_cfg["symbols"]
    starting_equity = float(universe_cfg.get("starting_cash", 100_000))

    broker = SimBroker(STATE_DIR / "broker.json", starting_cash=starting_equity)
    gate = RiskGate(RiskLimits.from_dict(risk_cfg))
    gate.start_cycle()
    mem = memory_mod.Memory(ROOT / "mandate" / "mandate.md", ROOT / "mandate" / "memory.md")

    if _kill_switch_active():
        return {
            "ts": now.isoformat(),
            "halted": "kill_switch",
            "decisions": [],
            "portfolio": None,
            "prices": {},
        }

    # Données marché : barres récentes par symbole (l'agent calcule SES indicateurs dessus).
    bars_by_symbol: dict[str, list] = {}
    prices: dict[str, float] = {}
    for sym in symbols:
        try:
            bars = market.get_bars(sym, lookback="5d", interval="1h")
            bars_by_symbol[sym] = bars
            prices[sym] = bars[-1].close
        except market.MarketError as e:
            log.warning("données indisponibles %s: %s", sym, e.code)

    snap = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity)
    gross = sum(abs(h.market_value) for h in snap.holdings)

    # Contexte cross-asset partagé : tout l'univers est visible à chaque décision
    # (l'edge de l'agent = relations entre symboles, pas un graphe isolé).
    base_context = {
        "now": now.isoformat(),
        "universe": symbols,
        "prices": {s: round(p, 4) for s, p in prices.items()},
        "portfolio": snap.as_context(),
        "bars": {s: _compact_bars(b) for s, b in bars_by_symbol.items()},
    }

    report: dict = {
        "ts": now.isoformat(),
        "dry_run": dry_run,
        "decisions": [],
        "portfolio": snap.as_context(),
        "prices": {s: round(p, 4) for s, p in prices.items()},
    }
    mandate_txt, memory_txt = mem.read_mandate(), mem.read_memory()

    for sym in symbols:
        if sym not in prices:
            continue
        ctx = {**base_context, "symbol": sym}  # le symbole à décider ce tour
        decision = codex_client.decide(symbol=sym, mandate=mandate_txt, memory=memory_txt, context=ctx)
        entry = {"symbol": sym, "action": decision.action, "qty": decision.quantity,
                 "confidence": decision.confidence, "rationale": decision.rationale}

        if decision.action == "HOLD" or decision.quantity == 0:
            report["decisions"].append({**entry, "executed": False, "reason": "hold"})
            continue

        order = Order(symbol=sym, side=decision.action, quantity=abs(decision.quantity), rationale=decision.rationale)
        pos = broker.positions().get(sym)
        cur_pos_value = (pos.quantity * prices[sym]) if pos else 0.0
        verdict = gate.check(order, prices[sym], current_position_value=cur_pos_value,
                             gross_exposure=gross, equity=snap.equity)

        if not verdict.approved:
            report["decisions"].append({**entry, "executed": False, "reason": f"risk:{verdict.code}", "context": verdict.context})
            continue

        fill = broker.submit(order, prices[sym], now.isoformat(), dry_run=dry_run)
        if not dry_run:
            gate.record_pass()
            gross += order.quantity * prices[sym]
        report["decisions"].append({**entry, "executed": not dry_run, "reason": "ok", "price": prices[sym]})

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="casys-trader daemon (boucle runtime)")
    parser.add_argument("--live", action="store_true", help="exécute réellement les ordres (défaut: dry-run)")
    parser.add_argument("--once", action="store_true", help="un seul cycle puis sortie")
    parser.add_argument("--poll", type=float, default=30.0, help="secondes entre deux vérifications du scheduler")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry_run = not args.live
    sched = scheduler.Scheduler(STATE_DIR / "scheduler.json")
    log.info("daemon démarré (dry_run=%s, once=%s)", dry_run, args.once)

    while True:
        report = run_cycle(dry_run=dry_run)
        log.info("cycle: %s", json.dumps(report, ensure_ascii=False))
        (STATE_DIR / "last_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

        if args.once:
            break

        wait = sched.seconds_until_wake()
        if wait <= 0:
            log.info("aucun réveil planifié par l'agent — pause %.0fs par défaut", args.poll)
            wait = args.poll
        time.sleep(min(wait, args.poll))


if __name__ == "__main__":
    main()
