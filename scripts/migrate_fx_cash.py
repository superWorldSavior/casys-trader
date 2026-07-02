"""Recalcule le cash du broker en USD + stampe fx_rate sur les sources d'historique.

dry_run par défaut. --commit pour écrire (avec backup).
Deux sources estampillées avec le MÊME `currency_for` canonique et les mêmes taux :
- `broker.json` (fills) → recalcule aussi le cash en USD ;
- `model_performance.jsonl` (rows) → source du P&L par trade (attribution).
Pour les lignes legacy sans fx_rate, applique le taux fourni (`rates`) et le
persiste (déterminisme futur).
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from trader.market import fx


def _backup_once(path: Path, tag: str) -> None:
    """Backup non destructif : n'écrase JAMAIS un backup existant (préserve l'original)."""
    bak = path.with_name(f"{path.name}.bak-pre-fx-{tag}")
    if not bak.exists():
        shutil.copyfile(path, bak)


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
        comm_ccy = fill.get("commission_currency") or sym_ccy
        comm_rate = float(fill.get("fx_rate") or rates.get(comm_ccy, 1.0)) if comm_ccy == sym_ccy else float(rates.get(comm_ccy, 1.0))
        cash -= fx.to_usd(float(fill.get("commission") or 0.0), comm_ccy, comm_rate)
    report = {"cash_before": data.get("cash"), "cash_after": cash, "n_fills": len(fills)}
    if commit:
        ts_tag = fills[-1]["ts"][:10] if fills else "init"
        _backup_once(Path(broker_path), ts_tag)
        data["cash"] = cash
        data["fills"] = fills
        Path(broker_path).write_text(json.dumps(data, indent=2))
    return report


def stamp_perf_rows(perf_path: Path, *, rates: dict[str, float], commit: bool) -> dict:
    """Stampe fx_rate sur chaque row de model_performance.jsonl (source attribution).

    Pas de recalcul de cash : l'attribution dérive le P&L des prix + fx_rate.
    Idempotent : ne touche pas une row qui a déjà un fx_rate.
    """
    perf_path = Path(perf_path)
    if not perf_path.exists():
        return {"perf_rows": 0, "stamped": 0, "by_ccy": {}}
    lines = perf_path.read_text().splitlines()
    out: list[str] = []
    stamped = 0
    by_ccy: dict[str, int] = {}
    last_ts = "init"
    for ln in lines:
        if not ln.strip():
            out.append(ln)
            continue
        row = json.loads(ln)
        last_ts = str(row.get("ts") or last_ts)
        if "symbol" in row and "fx_rate" not in row:
            ccy = fx.currency_for(row["symbol"])
            rate = float(rates.get(ccy, 1.0))
            row["fx_rate"] = rate
            stamped += 1
            by_ccy[ccy] = by_ccy.get(ccy, 0) + 1
        out.append(json.dumps(row, ensure_ascii=False))
    report = {"perf_rows": len([x for x in lines if x.strip()]), "stamped": stamped, "by_ccy": by_ccy}
    if commit and stamped:
        _backup_once(perf_path, last_ts[:10])
        perf_path.write_text("\n".join(out) + "\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state", help="dossier d'état (contient broker.json)")
    ap.add_argument("--starting-cash", type=float, default=100_000.0)
    ap.add_argument("--commit", action="store_true", help="écrire (défaut: dry-run)")
    args = ap.parse_args()
    from trader.market import fx_rates
    cfg = fx_rates.load_fx_config(Path("config/fx.yaml"))
    rates = {ccy: float(spec["fallback"]) for ccy, spec in cfg.items()}
    state = Path(args.state)
    broker_report = run(state / "broker.json", rates=rates,
                        starting_cash=args.starting_cash, commit=args.commit)
    perf_report = stamp_perf_rows(state / "model_performance.jsonl", rates=rates, commit=args.commit)
    print(json.dumps({"broker": broker_report, "model_performance": perf_report,
                      "committed": args.commit, "rates": rates}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
