"""Recalcule le cash du broker en USD depuis l'historique des fills.

dry_run par défaut. --commit pour écrire (avec backup).
Pour les fills legacy sans fx_rate, applique le taux fourni (`rates`) et le
persiste sur le fill (déterminisme futur).
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from trader import fx


def run(broker_path: Path, *, rates: dict[str, float], starting_cash: float, commit: bool) -> dict:
    data = json.loads(Path(broker_path).read_text())
    fills = data.get("fills", [])
    cash = starting_cash
    for fill in fills:
        sym_ccy = fx.currency_for(fill["symbol"])
        rate = float(fill.get("fx_rate") or rates.get(sym_ccy, 1.0))
        fill["fx_rate"] = rate
        signed = fill["quantity"] if fill["side"] == "BUY" else -fill["quantity"]
        cash -= fx.to_usd(signed * fill["price"], sym_ccy, rate)
        cash -= fx.to_usd(float(fill.get("commission") or 0.0),
                          fill.get("commission_currency") or sym_ccy, rate)
    report = {"cash_before": data.get("cash"), "cash_after": cash, "n_fills": len(fills)}
    if commit:
        ts_tag = fills[-1]["ts"][:10] if fills else "init"
        shutil.copyfile(broker_path, broker_path.with_name(f"{broker_path.name}.bak-pre-fx-{ts_tag}"))
        data["cash"] = cash
        data["fills"] = fills
        Path(broker_path).write_text(json.dumps(data, indent=2))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state", help="dossier d'état (contient broker.json)")
    ap.add_argument("--starting-cash", type=float, default=100_000.0)
    ap.add_argument("--commit", action="store_true", help="écrire (défaut: dry-run)")
    args = ap.parse_args()
    from trader import fx_rates
    cfg = fx_rates.load_fx_config(Path("config/fx.yaml"))
    rates = {ccy: float(spec["fallback"]) for ccy, spec in cfg.items()}
    report = run(Path(args.state) / "broker.json", rates=rates,
                 starting_cash=args.starting_cash, commit=args.commit)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
